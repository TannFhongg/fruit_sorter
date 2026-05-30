"""
tests/unit/test_event_bus.py
============================
Unit tests for shared/event_bus.py.

Covers:
  • Basic subscribe / emit / unsubscribe lifecycle
  • Idempotent subscription (no duplicate calls)
  • Callback exception isolation (one bad callback must not stop others)
  • Thread-safety: concurrent emitters and subscribers
  • EVT_* constant values match what producers / consumers use

Run: pytest tests/ -v
"""

from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock

import pytest

from shared.event_bus import EVT_DETECTION, EVT_FRAME, EVT_SORT_DONE, EventBus, bus


# ── Fixture: fresh bus per test (avoid cross-test pollution) ──────────────

@pytest.fixture
def eb() -> EventBus:
    return EventBus()


# ── Basic lifecycle ───────────────────────────────────────────────────────

class TestSubscribeEmit:

    def test_callback_called_on_emit(self, eb):
        cb = MagicMock()
        eb.subscribe("ping", cb)
        eb.emit("ping")
        cb.assert_called_once_with()

    def test_callback_receives_args(self, eb):
        cb = MagicMock()
        eb.subscribe("data", cb)
        eb.emit("data", 1, 2, key="val")
        cb.assert_called_once_with(1, 2, key="val")

    def test_emit_returns_call_count(self, eb):
        eb.subscribe("x", MagicMock())
        eb.subscribe("x", MagicMock())
        assert eb.emit("x") == 2

    def test_emit_unknown_event_is_noop(self, eb):
        assert eb.emit("no_such_event") == 0

    def test_multiple_callbacks_all_called(self, eb):
        results = []
        eb.subscribe("ev", lambda: results.append(1))
        eb.subscribe("ev", lambda: results.append(2))
        eb.emit("ev")
        assert sorted(results) == [1, 2]


class TestUnsubscribe:

    def test_unsubscribe_stops_calls(self, eb):
        cb = MagicMock()
        eb.subscribe("ev", cb)
        eb.unsubscribe("ev", cb)
        eb.emit("ev")
        cb.assert_not_called()

    def test_unsubscribe_nonexistent_is_noop(self, eb):
        eb.unsubscribe("ev", lambda: None)  # must not raise


class TestIdempotentSubscription:

    def test_duplicate_subscribe_calls_callback_once(self, eb):
        cb = MagicMock()
        eb.subscribe("ev", cb)
        eb.subscribe("ev", cb)   # second registration — should be ignored
        eb.emit("ev")
        cb.assert_called_once()


class TestClear:

    def test_clear_specific_event(self, eb):
        cb = MagicMock()
        eb.subscribe("ev1", cb)
        eb.subscribe("ev2", cb)
        eb.clear("ev1")
        eb.emit("ev1")
        cb.assert_not_called()
        eb.emit("ev2")
        cb.assert_called_once()

    def test_clear_all(self, eb):
        cb = MagicMock()
        eb.subscribe("a", cb)
        eb.subscribe("b", cb)
        eb.clear()
        eb.emit("a")
        eb.emit("b")
        cb.assert_not_called()


# ── Exception isolation ───────────────────────────────────────────────────

class TestExceptionIsolation:

    def test_bad_callback_does_not_kill_subsequent_callbacks(self, eb):
        good = MagicMock()

        def bad_cb():
            raise RuntimeError("intentional test error")

        eb.subscribe("ev", bad_cb)
        eb.subscribe("ev", good)
        eb.emit("ev")   # must not raise
        good.assert_called_once()

    def test_emit_does_not_propagate_exceptions(self, eb):
        eb.subscribe("ev", lambda: 1 / 0)
        # Should complete without raising
        count = eb.emit("ev")
        assert count == 0   # callback failed, so 0 successful calls


# ── Thread safety ─────────────────────────────────────────────────────────

class TestThreadSafety:

    def test_concurrent_emit_does_not_crash(self, eb):
        results = []
        lock    = threading.Lock()

        def cb(n: int) -> None:
            with lock:
                results.append(n)

        eb.subscribe("num", cb)

        threads = [threading.Thread(target=eb.emit, args=("num", i)) for i in range(50)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(results) == 50

    def test_subscribe_while_emitting_is_safe(self, eb):
        """
        A callback that subscribes another callback during emit must not
        deadlock (EventBus uses RLock for re-entrancy).
        """
        second = MagicMock()

        def first_cb():
            eb.subscribe("ev", second)  # re-entrant subscribe

        eb.subscribe("ev", first_cb)
        eb.emit("ev")   # must not deadlock

    def test_concurrent_subscribe_unsubscribe(self, eb):
        """Hammering subscribe/unsubscribe from many threads must not raise."""
        errors = []
        cb     = MagicMock()

        def worker():
            try:
                for _ in range(100):
                    eb.subscribe("ev", cb)
                    eb.emit("ev")
                    eb.unsubscribe("ev", cb)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == [], f"Errors during concurrent access: {errors}"


# ── Module-level singleton ────────────────────────────────────────────────

class TestSingleton:

    def test_bus_is_event_bus_instance(self):
        assert isinstance(bus, EventBus)

    def test_same_object_across_imports(self):
        from shared.event_bus import bus as bus2
        assert bus is bus2


# ── Canonical event name constants ───────────────────────────────────────

class TestEventNameConstants:

    def test_constants_are_strings(self):
        assert isinstance(EVT_FRAME,     str)
        assert isinstance(EVT_DETECTION, str)
        assert isinstance(EVT_SORT_DONE, str)

    def test_constants_are_distinct(self):
        names = {EVT_FRAME, EVT_DETECTION, EVT_SORT_DONE}
        assert len(names) == 3

    def test_producer_consumer_names_match(self, eb):
        """
        Simulates: fruit_detector emits EVT_DETECTION,
                   flask_app subscribes with the same constant.
        """
        received = []
        eb.subscribe(EVT_DETECTION, lambda label, confidence: received.append((label, confidence)))
        eb.emit(EVT_DETECTION, label="GREEN", confidence=0.91)
        assert received == [("GREEN", 0.91)]