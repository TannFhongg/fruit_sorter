"""
tests/unit/test_sort_controller.py
Kiểm tra SortController: FIFO order, timing gate, servo dispatch.

Chạy: pytest tests/ -v
"""

from __future__ import annotations

import threading
import time
from collections import deque
from unittest.mock import MagicMock

import pytest

from shared.detection_result import DetectionResult, FruitColor, SortAction


def _make_det(color: str, age_ms: float, action: SortAction) -> DetectionResult:
    det = DetectionResult(
        fruit_color=FruitColor(color),
        confidence=0.92,
        action=action,
        frame_id=1,
        bbox=(0, 0, 100, 100),
    )
    det.timestamp_ms = time.monotonic() * 1000 - age_ms
    return det


@pytest.fixture
def controller():
    cfg = {
        "conveyor": {"timing": {
            "ir1_window_ms": [700, 1000],
            "ir2_window_ms": [1200, 1800],
        }},
        "arduino": {
            "serial":    {"port": "/dev/ttyUSB0", "baudrate": 115200,
                          "timeout_s": 1.0, "reconnect_delay_s": 1.0,
                          "reconnect_max": 3},
            "heartbeat": {"interval_s": 5, "max_missed": 3},
        },
        "database": {},
    }
    serial_mock = MagicMock()
    serial_mock.is_connected = True
    serial_mock.send.return_value = True
    serial_mock.read_line.return_value = None

    q    = deque(maxlen=20)
    lock = threading.Lock()
    dbq  = deque(maxlen=100)
    stop = threading.Event()

    from control.sort_controller import SortController
    sc = SortController(
        cfg=cfg,
        serial_link=serial_mock,
        detection_queue=q,
        queue_lock=lock,
        db_write_queue=dbq,
        stop_event=stop,
    )
    return sc, q, lock, dbq, serial_mock


class TestFIFO:

    def test_oldest_consumed_first(self, controller):
        sc, q, *_ = controller
        q.append(_make_det("GREEN", 850, SortAction.SERVO1_LEFT))
        q.append(_make_det("RED",   200, SortAction.SERVO2_LEFT))

        sc._handle_ir_trigger({"ack": "IR_TRIGGER", "sensor": 1})

        assert len(q) == 1
        assert q[0].fruit_color == FruitColor.RED

    def test_empty_queue_safe(self, controller):
        sc, q, *_ = controller
        assert len(q) == 0
        sc._handle_ir_trigger({"ack": "IR_TRIGGER", "sensor": 1})
        assert len(q) == 0  # no crash


class TestTimingGate:

    def test_valid_window_consumes(self, controller):
        sc, q, _, dbq, serial = controller
        q.append(_make_det("GREEN", 850, SortAction.SERVO1_LEFT))
        sc._handle_ir_trigger({"ack": "IR_TRIGGER", "sensor": 1})
        assert len(q) == 0
        serial.send.assert_called_once()

    def test_too_early_blocked(self, controller):
        sc, q, _, _, serial = controller
        q.append(_make_det("GREEN", 400, SortAction.SERVO1_LEFT))  # 400 < 700
        sc._handle_ir_trigger({"ack": "IR_TRIGGER", "sensor": 1})
        assert len(q) == 1          # item NOT consumed
        serial.send.assert_not_called()

    def test_too_late_blocked(self, controller):
        sc, q, _, _, serial = controller
        q.append(_make_det("GREEN", 1200, SortAction.SERVO1_LEFT))  # 1200 > 1000
        sc._handle_ir_trigger({"ack": "IR_TRIGGER", "sensor": 1})
        assert len(q) == 1
        serial.send.assert_not_called()


class TestServoDispatch:

    # ĐỔI các SortAction trong test fixtures:
# SortAction.SERVO1_LEFT  → SortAction.SERVO1_FIRE
# SortAction.SERVO2_LEFT  → SortAction.SERVO2_FIRE
# SortAction.SERVO2_RIGHT → SortAction.SERVO2_FIRE
# SortAction.REJECT       → SortAction.PASS (cho RED/UNKNOWN hợp lệ)

@pytest.mark.parametrize("color,action,expected_servo,expected_dir", [
    ("GREEN",  SortAction.SERVO1_FIRE, 1, "fire"),   # ← ĐỔI
    ("YELLOW", SortAction.SERVO2_FIRE, 2, "fire"),   # ← ĐỔI
    # RED không còn trong list này — RED không kích servo
])
def test_correct_servo_command(...):
    ...
    assert cmd["dir"] == "fire"    # ← ĐỔI: "left"/"right" → "fire"

def test_red_no_servo_sent(self, controller):    # ← TEST MỚI
    sc, q, _, dbq, serial = controller
    q.append(_make_det("RED", 850, SortAction.PASS))
    sc._handle_ir_trigger({"ack": "IR_TRIGGER", "sensor": 1})
    serial.send.assert_not_called()
    assert dbq[-1].is_reject is False    # RED là PASS, không phải reject

def test_reject_no_servo_sent(self, controller):
    sc, q, _, dbq, serial = controller
    q.append(_make_det("UNKNOWN", 850, SortAction.REJECT))
    sc._handle_ir_trigger({"ack": "IR_TRIGGER", "sensor": 1})
    serial.send.assert_not_called()
    assert dbq[-1].is_reject is True    # UNKNOWN vẫn là reject thật


class TestDbQueue:

    def test_event_pushed_after_sort(self, controller):
        sc, q, _, dbq, _ = controller
        q.append(_make_det("RED", 850, SortAction.SERVO2_LEFT))
        sc._handle_ir_trigger({"ack": "IR_TRIGGER", "sensor": 1})
        assert len(dbq) == 1
        ev = dbq[0]
        assert ev.fruit_color == "RED"
        assert ev.station     == 1
        assert ev.is_reject   is False