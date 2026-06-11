from __future__ import annotations

from tools.calibrate_belt import (
    discard_pending_serial_messages,
    measure_sensor,
)


class _QueuedSerial:
    def __init__(self, messages: list[dict]) -> None:
        self.messages = list(messages)

    def read_message(self) -> dict | None:
        if not self.messages:
            return None
        return self.messages.pop(0)


def test_discard_pending_serial_messages_drains_existing_queue():
    serial = _QueuedSerial([
        {"ack": "IR_TRIGGER", "sensor": 1, "ts": 100},
        {"ack": "STATUS", "ok": True},
    ])

    drained = discard_pending_serial_messages(
        serial,
        quiet_s=0.0,
        timeout_s=0.1,
    )

    assert drained == 2
    assert serial.messages == []


def test_measure_sensor_discards_stale_trigger_before_start(monkeypatch):
    class SerialWithStaleThenFresh:
        def __init__(self) -> None:
            self.stale_returned = False
            self.drain_finished = False
            self.fresh_returned = False

        def read_message(self) -> dict | None:
            if not self.stale_returned:
                self.stale_returned = True
                return {"ack": "IR_TRIGGER", "sensor": 1, "ts": "stale"}
            if not self.drain_finished:
                self.drain_finished = True
                return None
            if not self.fresh_returned:
                self.fresh_returned = True
                return {"ack": "IR_TRIGGER", "sensor": 1, "ts": "fresh"}
            return None

    serial = SerialWithStaleThenFresh()
    monkeypatch.setattr("builtins.input", lambda _: "")

    times = measure_sensor(
        1,
        serial,
        1,
        pre_start_quiet_s=0.0,
        pre_start_timeout_s=0.1,
        trigger_timeout_s=0.1,
    )

    assert len(times) == 1
    assert serial.stale_returned is True
    assert serial.drain_finished is True
    assert serial.fresh_returned is True
