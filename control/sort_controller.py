"""
control/sort_controller.py
==========================
Thread 2 — nhận IR_TRIGGER từ Arduino Slave, khớp với DetectionResult
trong queue theo cửa sổ thời gian, kích servo sweep tương ứng.

v3.1 — Dynamic angle synchronization
=====================================
IMPORTANT CHANGE: Sweep angle giờ được đọc từ config YAML và gửi
trong mỗi lệnh SORT. Arduino không còn dùng #define hardcode nữa.

Điều này cho phép thay đổi góc quét (angle_sweep) trong file
config/hardware_config.yaml mà không cần biên dịch lại Arduino firmware.

Lệnh gửi xuống Arduino:
  {"cmd":"SORT","servo":1,"dir":"fire","angle":120}

v3.0 — SWEEP timing
====================
Với cơ chế quét (sweep), thời gian một chu kỳ servo là:
  sweep_duration_ms (200) + return_duration_ms (300) = 500 ms tổng.

Khi tính min_inter_fruit_interval (khoảng cách tối thiểu giữa 2 quả
cùng trạm), cần đảm bảo quả tiếp theo không đến khi sweep vẫn đang
chạy. Với belt 0.3 m/s và chu kỳ 500 ms → khoảng cách tối thiểu 15 cm.

Timing window (cửa sổ thời gian hợp lệ) tính từ lúc camera detect
đến lúc IR trigger: không thay đổi về công thức, chỉ phụ thuộc vào
khoảng cách camera→IR và tốc độ belt.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque

from drivers.serial_link import SerialLink
from shared.detection_result import DetectionResult, SortAction
from shared.event_bus import EVT_SORT_DONE, bus
from shared.serial_protocol import cmd_sort, is_ir_trigger, parse_response

log = logging.getLogger(__name__)


class SortController(threading.Thread):
    """
    Thread 2 — receives IR_TRIGGER events from the Arduino Slave,
    matches each event to the oldest pending DetectionResult in the
    shared detection queue, and triggers the correct servo sweep.
    """

    def __init__(
        self,
        cfg: dict,
        serial_link: SerialLink,
        detection_queue: deque,
        queue_lock: threading.Lock,
        db_write_queue: deque,
        stop_event: threading.Event,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self._cfg      = cfg
        self._serial   = serial_link
        self._queue    = detection_queue
        self._lock     = queue_lock
        self._db_queue = db_write_queue
        self._stop     = stop_event

        timing = cfg["conveyor"]["timing"]
        self._windows: dict[int, tuple[float, float]] = {
            1: tuple(timing.get("ir1_window_ms", [700,  1000])),
            2: tuple(timing.get("ir2_window_ms", [1200, 1800])),
        }

        # Read servo sweep angles from config (per-servo configuration)
        srv_cfg = cfg.get("hardware", {}).get("servos", {})
        s1 = srv_cfg.get("servo1", {})
        s2 = srv_cfg.get("servo2", {})
        
        self._servo_angles: dict[int, int] = {
            1: s1.get("angle_sweep", 120),
            2: s2.get("angle_sweep", 120),
        }

        # Total sweep cycle time per servo (sweep + return).
        # Used for logging/diagnostics only — the Arduino manages its own timer.
        self._sweep_cycle_ms = (
            s1.get("sweep_duration_ms", 200) +
            s1.get("return_duration_ms", 300)
        )
        log.info(
            "SortController init | windows=%s | angles=%s | sweep_cycle=%d ms",
            self._windows, self._servo_angles, self._sweep_cycle_ms,
        )

    # ── Main loop ──────────────────────────────────────────────────────────

    def run(self) -> None:
        log.info("SortController (T2) started — listening for IR triggers")
        while not self._stop.is_set():
            raw = self._serial.read_line()
            if not raw:
                time.sleep(0.001)
                continue

            msg = parse_response(raw)
            if msg and is_ir_trigger(msg):
                self._handle_ir_trigger(msg)

        log.info("SortController stopped")

    # ── IR trigger handler ─────────────────────────────────────────────────
    #
    # KEY DESIGN:
    #   • `now_ms` is computed INSIDE the lock — no preemption gap between
    #     this value and candidate.timestamp_ms read below.
    #   • Lock is released before calling _dispatch() — servo I/O never
    #     blocks queue access for other threads.
    #   • Popped `item` is exclusively owned by this thread.

    def _handle_ir_trigger(self, msg: dict) -> None:
        sensor_id = int(msg.get("sensor", 1))
        window    = self._windows.get(sensor_id, (0, 9999))

        item: DetectionResult | None = None
        with self._lock:
            if not self._queue:
                log.warning("IR%d triggered — queue empty, ignoring", sensor_id)
                return

            # now_ms inside lock → no stale-timestamp race
            now_ms = time.monotonic() * 1000

            # ── Purge expired detections ──────────────────────────────────
            max_window = max(w[1] for w in self._windows.values())
            purged_count = 0
            while self._queue:
                candidate = self._queue[0]
                delta_ms  = now_ms - candidate.timestamp_ms
                if delta_ms > max_window:
                    expired = self._queue.popleft()
                    purged_count += 1
                    log.warning(
                        "Purged expired detection: %s (age=%.0fms > max_window=%.0fms)",
                        expired.fruit_color.value, delta_ms, max_window,
                    )
                else:
                    break

            if purged_count > 0:
                log.info("Purged %d expired detection(s) from queue", purged_count)

            if not self._queue:
                log.warning("IR%d triggered — queue empty after purge", sensor_id)
                return

            candidate = self._queue[0]
            delta_ms  = now_ms - candidate.timestamp_ms

            # ── Timing window check ───────────────────────────────────────
            if not (window[0] <= delta_ms <= window[1]):
                if delta_ms > window[1]:
                    # Too late — decide whether to drop
                    should_drop = False
                    if sensor_id == 1:
                        should_drop = True
                    else:
                        all_earlier_missed = True
                        for sid in range(1, sensor_id):
                            earlier_window = self._windows.get(sid, (0, 9999))
                            if delta_ms <= earlier_window[1]:
                                all_earlier_missed = False
                                break
                        should_drop = all_earlier_missed

                    if should_drop:
                        missed = self._queue.popleft()
                        log.warning(
                            "IR%d: Dropping missed detection: %s "
                            "(delta=%.0fms > window[1]=%.0fms)",
                            sensor_id, missed.fruit_color.value,
                            delta_ms, window[1],
                        )
                        return

                log.warning(
                    "IR%d timing mismatch: delta=%.0fms, expected %.0f–%.0fms "
                    "(keeping in queue)",
                    sensor_id, delta_ms, window[0], window[1],
                )
                return

            # Valid timing — take exclusive ownership
            item = self._queue.popleft()
        # ── Lock released ─────────────────────────────────────────────────

        # ── Sensor-servo mapping validation ───────────────────────────────
        expected_servo_id = self._get_expected_servo(item)
        if expected_servo_id is not None and expected_servo_id != sensor_id:
            log.error(
                "IR%d: Sensor-servo mismatch! %s expects SERVO%d but "
                "triggered at IR%d. Fruit missed correct station. Dropping.",
                sensor_id, item.fruit_color.value,
                expected_servo_id, sensor_id,
            )
            return

        self._dispatch(sensor_id, item)

    def _get_expected_servo(self, item: DetectionResult) -> int | None:
        """Extract expected servo ID from item action.
        Returns None for PASS/REJECT actions (no servo needed)."""
        if item.action in (SortAction.PASS, SortAction.REJECT):
            return None
        parts = item.action.value.split("_")
        return int(parts[0].replace("SERVO", ""))

    # ── Dispatch ───────────────────────────────────────────────────────────
    #
    # Called with an exclusively-owned DetectionResult.
    # serial.send() is thread-safe via SerialLink's internal TX lock.

    def _dispatch(self, sensor_id: int, item: DetectionResult) -> None:
        is_pass   = item.action == SortAction.PASS
        is_reject = item.action == SortAction.REJECT

        if is_pass or is_reject:
            status = "PASS" if is_pass else "REJECT"
            log.info(
                "IR%d: %s → %s (no sweep)",
                sensor_id, item.fruit_color.value, status,
            )
        else:
            parts    = item.action.value.split("_")
            servo_id = int(parts[0].replace("SERVO", ""))
            
            # Get sweep angle from config for this servo
            sweep_angle = self._servo_angles.get(servo_id, 120)

            # Send SORT command with angle — Arduino will execute the sweep asynchronously
            ok     = self._serial.send(cmd_sort(servo_id, "fire", sweep_angle))
            status = "OK" if ok else "SERIAL_ERR"
            log.info(
                "IR%d: %s → SERVO%d SWEEP [angle=%d°] [conf=%.2f] [%s] "
                "(sweep_cycle~%d ms)",
                sensor_id, item.fruit_color.value,
                servo_id, sweep_angle, item.confidence, status,
                self._sweep_cycle_ms,
            )

        bus.emit(
            EVT_SORT_DONE,
            fruit_color=item.fruit_color.value,
            is_reject=(item.action == SortAction.REJECT),
        )
        self._push_db_event(item, sensor_id, (item.action == SortAction.REJECT))

    # ── DB event ───────────────────────────────────────────────────────────

    def _push_db_event(
        self,
        item: DetectionResult,
        station: int,
        is_reject: bool,
    ) -> None:
        from shared.detection_result import SortEvent  # local import — avoids cycle
        self._db_queue.append(
            SortEvent(
                fruit_color=item.fruit_color.value,
                confidence=item.confidence,
                action=item.action.value,
                station=station,
                is_reject=is_reject,
            )
        )