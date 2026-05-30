
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
    shared detection queue, and actuates the correct servo.
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
    # KEY DESIGN (updated):
    #   • `now_ms` is computed INSIDE the lock, immediately before reading
    #     `candidate.timestamp_ms`.  This eliminates the stale-timestamp
    #     race condition described in the module docstring.
    #   • Lock is still released before calling _dispatch(), so servo
    #     I/O never blocks queue access for other threads.
    #   • The popped `item` is a local variable exclusively owned by this
    #     thread from the moment the lock is released.

    def _handle_ir_trigger(self, msg: dict) -> None:
        sensor_id = int(msg.get("sensor", 1))
        window    = self._windows.get(sensor_id, (0, 9999))

        # ── Critical section: timestamp capture + inspect + (conditionally) pop
        item: DetectionResult | None = None
        with self._lock:
            if not self._queue:
                log.warning("IR%d triggered — queue empty, ignoring", sensor_id)
                return

            # ↓ now_ms computed INSIDE the lock — no preemption gap between
            #   this value and candidate.timestamp_ms read below.
            now_ms    = time.monotonic() * 1000
            
            # ── PURGE expired detections to prevent queue deadlock ────────
            # Remove all detections that are too old (beyond max window)
            max_window = max(w[1] for w in self._windows.values())
            purged_count = 0
            while self._queue:
                candidate = self._queue[0]
                delta_ms  = now_ms - candidate.timestamp_ms
                
                # If detection is too old (missed), remove it
                if delta_ms > max_window:
                    expired = self._queue.popleft()
                    purged_count += 1
                    log.warning(
                        "Purged expired detection: %s (age=%.0fms > max_window=%.0fms)",
                        expired.fruit_color.value, delta_ms, max_window
                    )
                else:
                    break  # Found a valid candidate, stop purging
            
            if purged_count > 0:
                log.info("Purged %d expired detection(s) from queue", purged_count)
            
            # After purging, check if queue is now empty
            if not self._queue:
                log.warning("IR%d triggered — queue empty after purge, ignoring", sensor_id)
                return
            
            # Get the next candidate after purging
            candidate = self._queue[0]
            delta_ms  = now_ms - candidate.timestamp_ms

            # ── Check timing window for current sensor ────────────────────
            if not (window[0] <= delta_ms <= window[1]):
                # Timing mismatch for this sensor.
                # 
                # CRITICAL: We must decide whether to:
                #   A) Keep the item (it might match a future sensor)
                #   B) Drop the item (it's too late for all sensors)
                #
                # Decision logic:
                #   - If delta_ms < window[0]: item is too early for this sensor.
                #     Keep it — might match this sensor on next trigger, or a
                #     later sensor.
                #   - If delta_ms > window[1]: item is too late for this sensor.
                #     Check if it's also too late for ALL earlier sensors.
                #     If yes, drop it (missed fruit).
                
                if delta_ms > window[1]:
                    # Item is too late for current sensor.
                    # Check if it's also too late for all earlier sensors.
                    # If sensor_id == 1, this is the first sensor, so drop.
                    # If sensor_id > 1, check if delta exceeds all earlier windows.
                    
                    should_drop = False
                    
                    if sensor_id == 1:
                        # First sensor - if too late here, fruit is missed
                        should_drop = True
                    else:
                        # Check if too late for all earlier sensors
                        # Example: IR2 triggered, delta=2000ms
                        # IR1 window is [700, 1000] → 2000 > 1000 → too late
                        all_earlier_missed = True
                        for sid in range(1, sensor_id):
                            earlier_window = self._windows.get(sid, (0, 9999))
                            if delta_ms <= earlier_window[1]:
                                # Still within window of an earlier sensor
                                all_earlier_missed = False
                                break
                        should_drop = all_earlier_missed
                    
                    if should_drop:
                        missed = self._queue.popleft()
                        log.warning(
                            "IR%d: Dropping missed detection: %s (delta=%.0fms > window[1]=%.0fms)",
                            sensor_id, missed.fruit_color.value, delta_ms, window[1]
                        )
                        return
                
                # Item is too early, or too late but might match earlier sensor
                log.warning(
                    "IR%d timing mismatch: delta=%.0fms, expected %.0f–%.0fms (keeping in queue)",
                    sensor_id, delta_ms, window[0], window[1],
                )
                return

            # Timing is valid — take exclusive ownership before releasing lock
            item = self._queue.popleft()
        # ── Lock released here; `item` is now thread-local ────────────────

        self._dispatch(sensor_id, item)

    # ── Dispatch ───────────────────────────────────────────────────────────
    #
    # Called with an exclusively-owned DetectionResult.
    # No shared mutable state is accessed here (serial.send() is itself
    # thread-safe via its own internal lock inside SerialLink).

    def _dispatch(self, sensor_id: int, item: DetectionResult) -> None:
        is_pass   = item.action == SortAction.PASS
        is_reject = item.action == SortAction.REJECT

        if is_pass or is_reject:
            status = "PASS" if is_pass else "REJECT"
            log.info("IR%d: %s → %s (no servo)", sensor_id, item.fruit_color.value, status)
        else:
            parts    = item.action.value.split("_")
            servo_id = int(parts[0].replace("SERVO", ""))
            ok       = self._serial.send(cmd_sort(servo_id, "fire"))
            status   = "OK" if ok else "SERIAL_ERR"
            log.info("IR%d: %s → SERVO%d FIRE [conf=%.2f] [%s]",
                 sensor_id, item.fruit_color.value,
                 servo_id, item.confidence, status)

        bus.emit(EVT_SORT_DONE,
             fruit_color=item.fruit_color.value,
             is_reject=(item.action == SortAction.REJECT))
        self._push_db_event(item, sensor_id, (item.action == SortAction.REJECT))

    # ── DB event ───────────────────────────────────────────────────────────

    def _push_db_event(
        self,
        item: DetectionResult,
        station: int,
        is_reject: bool,
    ) -> None:
        from shared.detection_result import SortEvent  # local import — avoids top-level cycle
        self._db_queue.append(
            SortEvent(
                fruit_color=item.fruit_color.value,
                confidence=item.confidence,
                action=item.action.value,
                station=station,
                is_reject=is_reject,
            )
        )