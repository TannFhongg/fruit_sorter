"""
control/sort_controller.py
==========================
Thread 2 — nhận IR_TRIGGER từ Arduino Slave, khớp với DetectionResult
trong queue theo cửa sổ thời gian, kích servo sweep tương ứng.

v3.3 — Dynamic 270° servo angle/home/timing synchronization
============================================================
IMPORTANT CHANGES:
  1. Home + sweep angle được đọc từ config YAML và gửi trong mỗi lệnh SORT
  2. Servo range/PWM calibration cho servo 270° cũng được gửi xuống Arduino
  3. Sweep timing (sweep_duration_ms, return_duration_ms) cũng được
     đọc từ config và gửi trong mỗi lệnh SORT

Arduino không còn dùng #define hardcode cho runtime góc và thời gian nữa.

Điều này cho phép thay đổi:
  - Góc nghỉ (angle_home): ví dụ servo1 nghỉ ở 220°
  - Góc quét (angle_sweep): ví dụ 120° → 180° để lực gạt mạnh hơn
  - Dải servo vật lý (angle_max): ví dụ 270° servo
  - Thời gian quét (sweep_duration_ms): phải tăng tương ứng với góc
    để servo có đủ thời gian hoàn thành hành trình
  - Thời gian về (return_duration_ms): điều chỉnh tốc độ trở về

...tất cả trong file config/hardware_config.yaml mà không cần biên dịch 
lại Arduino firmware.

PHYSICAL CONSISTENCY:
  Nếu tăng angle_sweep (ví dụ: 120° → 180°), BẮT BUỘC phải tăng
  sweep_duration_ms tương ứng. Nếu không, servo MG996R sẽ chỉ quay
  được một phần góc rồi bị ép quay về, gây mất đồng bộ vật lý.

Lệnh gửi xuống Arduino:
  {"cmd":"SORT","servo":1,"dir":"fire","angle":0,"home":220,"sweep_ms":200,"return_ms":300,"max":270,"min_us":500,"max_us":2500}

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
from shared.detection_result import DetectionResult, SortAction, SortEvent
from shared.event_bus import EVT_SORT_DONE, bus
from shared.serial_protocol import cmd_sort, is_ir_trigger

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

        # Read servo angles/calibration from config (per-servo configuration)
        srv_cfg = cfg.get("hardware", {}).get("servos", {})
        s1 = srv_cfg.get("servo1", {})
        s2 = srv_cfg.get("servo2", {})

        self._servo_home_angles: dict[int, int] = {
            1: s1.get("angle_home", 0),
            2: s2.get("angle_home", 0),
        }
        
        self._servo_angles: dict[int, int] = {
            1: s1.get("angle_sweep", 120),
            2: s2.get("angle_sweep", 120),
        }

        self._servo_angle_max: dict[int, int] = {
            1: s1.get("angle_max", 270),
            2: s2.get("angle_max", 270),
        }

        self._servo_pulse_min_us: dict[int, int] = {
            1: s1.get("pulse_min_us", 500),
            2: s2.get("pulse_min_us", 500),
        }
        self._servo_pulse_max_us: dict[int, int] = {
            1: s1.get("pulse_max_us", 2500),
            2: s2.get("pulse_max_us", 2500),
        }

        # Read servo timing parameters from config.
        self._servo_sweep_ms: dict[int, int] = {
            1: s1.get("sweep_duration_ms", 200),
            2: s2.get("sweep_duration_ms", 200),
        }
        self._servo_return_ms: dict[int, int] = {
            1: s1.get("return_duration_ms", 300),
            2: s2.get("return_duration_ms", 300),
        }

        # Total sweep cycle time per servo (sweep + return).
        # Used for logging/diagnostics only — the Arduino manages its own timer.
        self._sweep_cycle_ms = (
            s1.get("sweep_duration_ms", 200) +
            s1.get("return_duration_ms", 300)
        )
        log.info(
            "SortController init | windows=%s | home=%s | sweep=%s | max=%s "
            "| pulse=%s/%s | timing=%s/%s | cycle=%d ms",
            self._windows, self._servo_home_angles, self._servo_angles,
            self._servo_angle_max, self._servo_pulse_min_us,
            self._servo_pulse_max_us, self._servo_sweep_ms,
            self._servo_return_ms, self._sweep_cycle_ms,
        )

    # ── Main loop ──────────────────────────────────────────────────────────

    def run(self) -> None:
        log.info("SortController (T2) started — listening for IR triggers")
        while not self._stop.is_set():
            msg = self._serial.read_message()
            if not msg:
                time.sleep(0.001)
                continue

            if is_ir_trigger(msg):
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

            idx = 0
            while idx < len(self._queue):
                candidate = self._queue[idx]
                delta_ms  = now_ms - candidate.timestamp_ms
                expected_servo_id = self._get_expected_servo(candidate)

                # A fruit assigned to a downstream servo must pass upstream IR
                # sensors without being consumed. Keep scanning so it cannot
                # block a later fruit that belongs to this station.
                if expected_servo_id is not None:
                    if expected_servo_id > sensor_id:
                        log.debug(
                            "IR%d: %s expects SERVO%d downstream; keeping in queue "
                            "(age=%.0fms)",
                            sensor_id, candidate.fruit_color.value,
                            expected_servo_id, delta_ms,
                        )
                        idx += 1
                        continue

                    if expected_servo_id < sensor_id:
                        missed = candidate
                        del self._queue[idx]
                        log.error(
                            "IR%d: %s expected SERVO%d upstream and reached IR%d. "
                            "Dropping missed fruit.",
                            sensor_id, missed.fruit_color.value,
                            expected_servo_id, sensor_id,
                        )
                        continue

                # ── Timing window check ───────────────────────────────────
                if not (window[0] <= delta_ms <= window[1]):
                    if delta_ms > window[1]:
                        missed = candidate
                        del self._queue[idx]
                        log.warning(
                            "IR%d: Dropping missed detection: %s "
                            "(delta=%.0fms > window[1]=%.0fms)",
                            sensor_id, missed.fruit_color.value,
                            delta_ms, window[1],
                        )
                        continue

                    log.warning(
                        "IR%d timing mismatch: delta=%.0fms, expected %.0f–%.0fms "
                        "(keeping in queue)",
                        sensor_id, delta_ms, window[0], window[1],
                    )
                    return

                # Valid timing — take exclusive ownership of exactly this item
                item = candidate
                del self._queue[idx]
                break

            if item is None:
                log.debug(
                    "IR%d triggered — no matching detection for this sensor",
                    sensor_id,
                )
                return
        # ── Lock released ─────────────────────────────────────────────────

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
            
            # Get angle, calibration, and timing from config for this servo.
            home_angle = self._servo_home_angles.get(servo_id, 0)
            sweep_angle = self._servo_angles.get(servo_id, 120)
            sweep_ms    = self._servo_sweep_ms.get(servo_id, 200)
            return_ms   = self._servo_return_ms.get(servo_id, 300)
            angle_max   = self._servo_angle_max.get(servo_id, 270)
            pulse_min_us = self._servo_pulse_min_us.get(servo_id, 500)
            pulse_max_us = self._servo_pulse_max_us.get(servo_id, 2500)

            # Send SORT command with full config; Arduino executes the sweep asynchronously.
            ok = self._serial.send(cmd_sort(
                servo_id,
                "fire",
                sweep_angle,
                sweep_ms,
                return_ms,
                home_angle=home_angle,
                angle_max=angle_max,
                pulse_min_us=pulse_min_us,
                pulse_max_us=pulse_max_us,
            ))
            if not ok:
                log.error(
                    "IR%d: %s → SERVO%d command failed; not emitting sort-done "
                    "or writing DB event",
                    sensor_id, item.fruit_color.value, servo_id,
                )
                return

            log.info(
                "IR%d: %s → SERVO%d SWEEP [home=%d° sweep=%d° max=%d°] "
                "[%d-%dus] [%dms/%dms] [conf=%.2f] [OK]",
                sensor_id, item.fruit_color.value,
                servo_id, home_angle, sweep_angle, angle_max,
                pulse_min_us, pulse_max_us, sweep_ms, return_ms,
                item.confidence,
            )

        sort_event = self._build_sort_event(
            item,
            sensor_id,
            (item.action == SortAction.REJECT),
        )
        bus.emit(
            EVT_SORT_DONE,
            fruit_color=sort_event.fruit_color,
            confidence=sort_event.confidence,
            action=sort_event.action,
            station=sort_event.station,
            is_reject=sort_event.is_reject,
            ts_ms=sort_event.sorted_at_ms,
        )
        self._push_db_event(sort_event)

    # ── DB event ───────────────────────────────────────────────────────────

    def _build_sort_event(
        self,
        item: DetectionResult,
        station: int,
        is_reject: bool,
    ) -> SortEvent:
        return SortEvent(
            fruit_color=item.fruit_color.value,
            confidence=item.confidence,
            action=item.action.value,
            station=station,
            is_reject=is_reject,
        )

    def _push_db_event(self, event: SortEvent) -> None:
        self._db_queue.append(event)
