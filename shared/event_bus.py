"""
shared/event_bus.py
===================
Lightweight, thread-safe publish/subscribe bus for intra-process events.

Replaces direct cross-module imports between perception and web layers.
Any module can register a callback; any module can emit an event.
No module needs to import any other application module.

Usage
-----
Producer (fruit_detector.py):
    from shared.event_bus import bus
    bus.emit("frame",     jpeg_bytes)
    bus.emit("detection", label="GREEN", confidence=0.91)

Consumer (flask_app.py — called once at startup via main.py):
    from shared.event_bus import bus
    bus.subscribe("frame",     _on_frame)
    bus.subscribe("detection", _on_detection)

Consumer (sort_controller.py — for live-count updates):
    bus.subscribe("sort_done", _on_sort_done)

Design decisions
----------------
* Callbacks execute **in the emitter's thread** (no hidden thread spawning).
  Keep callbacks short — do not block inside them.
* A failing callback is caught and logged; it never kills the emitter.
* Subscribing/unsubscribing is protected by a re-entrant lock so it is safe
  to subscribe from within a callback (e.g. one-shot handlers).
"""

from __future__ import annotations

import logging
import threading
from collections import defaultdict
from typing import Any, Callable

log = logging.getLogger(__name__)


class EventBus:
    """
    Thread-safe, synchronous publish/subscribe event bus.

    All registered callbacks for a given event name are called
    sequentially in the same thread that calls emit().
    """

    def __init__(self) -> None:
        # event_name -> list of callbacks
        self._listeners: dict[str, list[Callable]] = defaultdict(list)
        self._lock = threading.RLock()  # RLock: safe to subscribe inside a callback

    # ── Subscription API ──────────────────────────────────────────────────

    def subscribe(self, event: str, callback: Callable) -> None:
        """Register *callback* for *event*. Idempotent — duplicate registrations
        are silently ignored so calling subscribe() multiple times (e.g. on
        hot-reload) does not multiply callbacks."""
        with self._lock:
            if callback not in self._listeners[event]:
                self._listeners[event].append(callback)
                log.debug(f"EventBus: subscribed {callback.__qualname__!r} → '{event}'")

    def unsubscribe(self, event: str, callback: Callable) -> None:
        """Remove *callback* from *event*. No-op if not registered."""
        with self._lock:
            try:
                self._listeners[event].remove(callback)
            except ValueError:
                pass

    def clear(self, event: str | None = None) -> None:
        """Remove all callbacks for *event*, or for every event if None."""
        with self._lock:
            if event is None:
                self._listeners.clear()
            else:
                self._listeners.pop(event, None)

    # ── Emit API ──────────────────────────────────────────────────────────

    def emit(self, event: str, *args: Any, **kwargs: Any) -> int:
        """
        Call all callbacks registered for *event* with the given arguments.

        Returns the number of callbacks that were called successfully.
        Exceptions inside callbacks are caught, logged, and skipped so that
        a broken listener never interrupts the emitter.
        """
        with self._lock:
            # Snapshot the list so that subscribe/unsubscribe during emit is safe
            callbacks = list(self._listeners.get(event, []))

        called = 0
        for cb in callbacks:
            try:
                cb(*args, **kwargs)
                called += 1
            except Exception:
                log.exception(
                    f"EventBus: unhandled exception in callback "
                    f"{cb.__qualname__!r} for event '{event}'"
                )
        return called

    # ── Introspection ─────────────────────────────────────────────────────

    def listeners(self, event: str) -> list[Callable]:
        """Return a snapshot of registered callbacks for *event*."""
        with self._lock:
            return list(self._listeners.get(event, []))

    def __repr__(self) -> str:
        with self._lock:
            summary = {k: len(v) for k, v in self._listeners.items()}
        return f"EventBus({summary})"


# ── Module-level singleton ─────────────────────────────────────────────────
#
# Import this object everywhere:
#   from shared.event_bus import bus
#
# It is created once at import time.  Because Python's import system
# caches modules, every importer gets the same instance.
#
bus: EventBus = EventBus()

# ── Canonical event names (constants to avoid typos) ─────────────────────
#
# Use these instead of bare strings where possible:
#
EVT_FRAME      = "frame"       # payload: jpeg_bytes: bytes
EVT_DETECTION  = "detection"   # payload: label: str, confidence: float
EVT_SORT_DONE  = "sort_done"   # payload: fruit_color: str, is_reject: bool