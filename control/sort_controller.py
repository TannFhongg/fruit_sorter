"""
control/sort_controller.py  ·  Thread 2 — Control
===================================================
Listens for IR_TRIGGER messages from the Arduino Slave via Serial,
dequeues a DetectionResult, validates the timing window, sends the
appropriate SORT command, and records a SortEvent to the DB queue.

Thread-safety fix (Race Condition #1) — ORIGINAL
--------------------------------------------------
Previous code accessed queue[0] and queue.popleft() inside the lock,
but then called _dispatch() — which touches further shared state —
*outside* the lock while still holding a reference to the dequeued
object.

Fixed pattern:
  1. Acquire the lock.
  2. Peek, validate timing, and popleft() — all inside the lock.
  3. Immediately assign the popped item to a *local variable*.
  4. Release the lock (exit the `with` block).
  5. Call _dispatch(item) with the now-exclusively-owned local copy.

Bug fix (Race Condition #2) — now_ms stale timestamp
------------------------------------------------------
PROBLEM: In the previous version, `now_ms` was computed BEFORE
acquiring the lock:

    now_ms = time.monotonic() * 1000   # ← captured here
    window = self._windows.get(...)

    with self._lock:                   # ← lock acquired AFTER
        candidate = self._queue[0]
        delta_ms  = now_ms - candidate.timestamp_ms

Between computing `now_ms` and reading `candidate.timestamp_ms`,
the OS scheduler can preempt this thread for an arbitrary duration
(typically 1–50 ms on a loaded RPi 4).  On a conveyor running at
0.3 m/s with a ±20 % timing window (~±170 ms), a 30–50 ms stale
timestamp is enough to either:
  • Pass a fruit that should have been rejected (delta appears in-window
    when it is actually out-of-window), or
  • Block a valid fruit (delta appears out-of-window due to inflated
    elapsed time).

FIXED PATTERN:
  `now_ms` is now computed INSIDE the lock, immediately before
  `candidate.timestamp_ms` is read.  Both values are captured in the
  same critical section so no other thread can interleave between them.

    with self._lock:
        if not self._queue:
            return
        now_ms    = time.monotonic() * 1000   # ← inside lock
        candidate = self._queue[0]
        delta_ms  = now_ms - candidate.timestamp_ms
        ...

Because `time.monotonic()` is a simple syscall with no side-effects on
shared state, holding the lock while calling it is safe and adds
negligible latency (< 1 µs).

Circular-import fix (Issue #3)
-------------------------------
The previous version imported `update_live_count` directly from
`web.flask_app`, creating a hidden circular dependency.  That call is
replaced with a publish on the module-level `bus` singleton from
`shared.event_bus`.  `flask_app` subscribes to EVT_SORT_DONE at
startup (wired in main.py).
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
            candidate = self._queue[0]
            delta_ms  = now_ms - candidate.timestamp_ms

            if not (window[0] <= delta_ms <= window[1]):
                log.warning(
                    "IR%d timing mismatch: delta=%.0fms, expected %.0f–%.0fms",
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
        is_reject = item.action == SortAction.REJECT

        if is_reject:
            log.info("IR%d: %s → REJECT", sensor_id, item.fruit_color.value)
        else:
            # SortAction values follow the pattern "SERVO{n}_{DIRECTION}"
            parts     = item.action.value.split("_")   # e.g. ["SERVO1", "LEFT"]
            servo_id  = int(parts[0].replace("SERVO", ""))
            direction = parts[1].lower()

            ok     = self._serial.send(cmd_sort(servo_id, direction))
            status = "OK" if ok else "SERIAL_ERR"
            log.info(
                "IR%d: %s → SERVO%d %s [conf=%.2f] [%s]",
                sensor_id, item.fruit_color.value,
                servo_id, direction.upper(),
                item.confidence, status,
            )

        # Publish sort outcome on the event bus.
        # flask_app subscribes to EVT_SORT_DONE to update live counters.
        # No direct import of flask_app here — circular dependency is gone.
        bus.emit(EVT_SORT_DONE, fruit_color=item.fruit_color.value, is_reject=is_reject)

        # Persist the event asynchronously via the DB write queue
        self._push_db_event(item, sensor_id, is_reject)

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