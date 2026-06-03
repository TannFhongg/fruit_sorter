"""
tests/unit/test_sort_controller.py
===================================
Kiểm tra SortController: FIFO order, timing gate, servo dispatch.

v3.0 — Sweep mechanism: servo nhận lệnh "fire" và tự thực hiện
sweep 0°→120°→0° trên Arduino. RPi chỉ gửi cmd_sort(servo_id, "fire").
Các test case không thay đổi về logic — chỉ cập nhật comment.

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
    """
    Tạo DetectionResult với timestamp đã cũ đi age_ms so với hiện tại.

    Args:
        color:   Màu quả (GREEN, RED, YELLOW, UNKNOWN)
        age_ms:  Bao nhiêu ms trước (so với time.monotonic() * 1000)
        action:  SortAction được gán

    Returns:
        DetectionResult với timestamp = now - age_ms
    """
    timestamp_ms = time.monotonic() * 1000 - age_ms
    return DetectionResult(
        fruit_color=FruitColor(color),
        confidence=0.92,
        action=action,
        frame_id=1,
        bbox=(0, 0, 100, 100),
        timestamp_ms=timestamp_ms,
    )


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
        "hardware": {
            "servos": {
                "servo1": {
                    "angle_home": 0, "angle_sweep": 120,
                    "sweep_duration_ms": 200, "return_duration_ms": 300,
                },
                "servo2": {
                    "angle_home": 0, "angle_sweep": 120,
                    "sweep_duration_ms": 200, "return_duration_ms": 300,
                },
            }
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


# ── FIFO order ────────────────────────────────────────────────────────────

class TestFIFO:

    def test_oldest_consumed_first(self, controller):
        """Queue phải tiêu thụ item cũ nhất (popleft) trước."""
        sc, q, *_ = controller
        q.append(_make_det("GREEN", 850, SortAction.SERVO1_FIRE))
        q.append(_make_det("RED",   200, SortAction.PASS))

        sc._handle_ir_trigger({"ack": "IR_TRIGGER", "sensor": 1})

        assert len(q) == 1
        assert q[0].fruit_color == FruitColor.RED

    def test_empty_queue_safe(self, controller):
        """Không crash khi queue rỗng."""
        sc, q, *_ = controller
        assert len(q) == 0
        sc._handle_ir_trigger({"ack": "IR_TRIGGER", "sensor": 1})
        assert len(q) == 0


# ── Timing gate ───────────────────────────────────────────────────────────

class TestTimingGate:

    def test_valid_window_consumes(self, controller):
        """Item 850ms tuổi nằm trong cửa sổ [700,1000] của IR1 → tiêu thụ."""
        sc, q, _, dbq, serial = controller
        q.append(_make_det("GREEN", 850, SortAction.SERVO1_FIRE))
        sc._handle_ir_trigger({"ack": "IR_TRIGGER", "sensor": 1})
        assert len(q) == 0
        serial.send.assert_called_once()

    def test_too_early_blocked(self, controller):
        """400ms < 700ms (cận dưới) → item bị giữ lại trong queue."""
        sc, q, _, _, serial = controller
        q.append(_make_det("GREEN", 400, SortAction.SERVO1_FIRE))
        sc._handle_ir_trigger({"ack": "IR_TRIGGER", "sensor": 1})
        assert len(q) == 1          # item NOT consumed
        serial.send.assert_not_called()

    def test_too_late_dropped(self, controller):
        """1200ms > 1000ms (cận trên) ở IR1 (sensor đầu tiên) → item bị drop."""
        sc, q, _, _, serial = controller
        q.append(_make_det("GREEN", 1200, SortAction.SERVO1_FIRE))
        sc._handle_ir_trigger({"ack": "IR_TRIGGER", "sensor": 1})
        assert len(q) == 0          # item dropped (missed fruit)
        serial.send.assert_not_called()


# ── Sweep dispatch — lệnh gửi xuống Arduino ───────────────────────────────

class TestSweepDispatch:

    def test_green_servo1_sweep_command(self, controller):
        """
        GREEN → SERVO1_FIRE, trigger qua IR1 (sensor=1).
        Lệnh gửi Arduino: SORT servo=1 dir=fire.
        Arduino tự thực hiện sweep 0°→120°→0°.
        """
        sc, q, _, _, serial = controller
        q.append(_make_det("GREEN", 850, SortAction.SERVO1_FIRE))
        # IR1 (sensor=1) phục vụ SERVO1 → timing window ir1=[700,1000]
        sc._handle_ir_trigger({"ack": "IR_TRIGGER", "sensor": 1})

        import json
        assert serial.send.called, "serial.send() phải được gọi cho GREEN"
        call_bytes = serial.send.call_args[0][0]
        cmd = json.loads(call_bytes.decode().strip())
        assert cmd["cmd"]   == "SORT"
        assert cmd["servo"] == 1
        assert cmd["dir"]   == "fire"

    def test_yellow_servo2_sweep_command(self, controller):
        """
        YELLOW → SERVO2_FIRE, trigger qua IR2 (sensor=2).
        Lệnh gửi Arduino: SORT servo=2 dir=fire.

        Vật lý: IR2 nằm sau IR1 trên băng chuyền, phục vụ SERVO2.
        Timing window của IR2: [1200, 1800]ms (xa camera hơn IR1).
        """
        sc, q, _, _, serial = controller
        q.append(_make_det("YELLOW", 1500, SortAction.SERVO2_FIRE))
        # IR2 (sensor=2) phục vụ SERVO2 → timing window ir2=[1200,1800]
        sc._handle_ir_trigger({"ack": "IR_TRIGGER", "sensor": 2})

        import json
        assert serial.send.called, "serial.send() phải được gọi cho YELLOW"
        call_bytes = serial.send.call_args[0][0]
        cmd = json.loads(call_bytes.decode().strip())
        assert cmd["cmd"]   == "SORT"
        assert cmd["servo"] == 2
        assert cmd["dir"]   == "fire"

    def test_red_no_servo_sent(self, controller):
        """Quả đỏ → PASS → không gửi lệnh SORT."""
        sc, q, _, dbq, serial = controller
        q.append(_make_det("RED", 850, SortAction.PASS))
        sc._handle_ir_trigger({"ack": "IR_TRIGGER", "sensor": 1})
        serial.send.assert_not_called()
        assert dbq[-1].is_reject is False

    def test_reject_no_servo_sent(self, controller):
        """UNKNOWN → REJECT → không gửi lệnh SORT."""
        sc, q, _, dbq, serial = controller
        q.append(_make_det("UNKNOWN", 850, SortAction.REJECT))
        sc._handle_ir_trigger({"ack": "IR_TRIGGER", "sensor": 1})
        serial.send.assert_not_called()
        assert dbq[-1].is_reject is True

    def test_serial_failure_logged(self, controller):
        """
        Nếu serial.send() trả False (lỗi kết nối), sort vẫn hoàn thành
        (event bus và DB vẫn được cập nhật); chỉ log SERIAL_ERR.
        """
        sc, q, _, dbq, serial = controller
        serial.send.return_value = False
        q.append(_make_det("GREEN", 850, SortAction.SERVO1_FIRE))
        sc._handle_ir_trigger({"ack": "IR_TRIGGER", "sensor": 1})
        # DB event vẫn được push dù serial fail
        assert len(dbq) == 1
        assert dbq[0].fruit_color == "GREEN"


# ── Sensor-servo mismatch ─────────────────────────────────────────────────

class TestSensorServoMismatch:

    def test_mismatch_drops_item(self, controller):
        """
        SERVO2_FIRE ở IR1 → mismatch (IR1 phục vụ SERVO1).
        Item bị drop, không gửi serial.
        """
        sc, q, _, dbq, serial = controller
        # SERVO2_FIRE nhưng IR1 trigger → expected_servo=2 ≠ sensor_id=1
        q.append(_make_det("YELLOW", 850, SortAction.SERVO2_FIRE))
        sc._handle_ir_trigger({"ack": "IR_TRIGGER", "sensor": 1})
        serial.send.assert_not_called()
        assert len(dbq) == 0  # không push DB khi mismatch


# ── DB queue ──────────────────────────────────────────────────────────────

class TestDbQueue:

    def test_event_pushed_after_sort(self, controller):
        """SortEvent phải được push vào db_write_queue sau mỗi lần sort."""
        sc, q, _, dbq, _ = controller
        q.append(_make_det("RED", 850, SortAction.PASS))
        sc._handle_ir_trigger({"ack": "IR_TRIGGER", "sensor": 1})
        assert len(dbq) == 1
        ev = dbq[0]
        assert ev.fruit_color == "RED"
        assert ev.station     == 1
        assert ev.is_reject   is False

    def test_sweep_action_recorded_in_db(self, controller):
        """SortEvent với action SERVO1_FIRE phải ghi đúng vào DB queue."""
        sc, q, _, dbq, _ = controller
        q.append(_make_det("GREEN", 850, SortAction.SERVO1_FIRE))
        sc._handle_ir_trigger({"ack": "IR_TRIGGER", "sensor": 1})
        assert len(dbq) == 1
        ev = dbq[0]
        assert ev.fruit_color == "GREEN"
        assert ev.action      == "SERVO1_FIRE"
        assert ev.is_reject   is False


# ── Queue purge ───────────────────────────────────────────────────────────

class TestQueuePurge:

    def test_expired_detection_purged(self, controller):
        """
        Detection cũ hơn max_window bị purge tự động khi IR trigger đến.
        max_window = max(ir1_window[1]=1000, ir2_window[1]=1800) = 1800ms
        """
        sc, q, *_ = controller
        # 2500ms >> 1800ms → sẽ bị purge
        q.append(_make_det("GREEN", 2500, SortAction.SERVO1_FIRE))
        # 850ms → valid cho IR1
        q.append(_make_det("RED",    850, SortAction.PASS))

        sc._handle_ir_trigger({"ack": "IR_TRIGGER", "sensor": 1})
        # GREEN bị purge, RED được consume → queue rỗng
        assert len(q) == 0