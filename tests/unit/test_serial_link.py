from __future__ import annotations

import threading

from drivers.serial_link import SerialLink
from shared.serial_protocol import cmd_ping


def _cfg() -> dict:
    return {
        "system": {"serial_event_queue_maxlen": 10},
        "arduino": {
            "serial": {
                "port": "/dev/ttyUSB0",
                "baudrate": 115200,
                "timeout_s": 1.0,
                "reconnect_delay_s": 1.0,
                "reconnect_max": 3,
            },
            "heartbeat": {"interval_s": 5, "max_missed": 3},
        },
    }


class _FakeSerial:
    def __init__(self) -> None:
        self.writes: list[bytes] = []

    def write(self, data: bytes) -> None:
        self.writes.append(data)

    def readline(self) -> bytes:
        raise AssertionError("heartbeat must not read the UART directly")

    def close(self) -> None:
        pass


def test_pong_consumed_and_ir_trigger_queued():
    link = SerialLink(_cfg(), threading.Event())

    link._handle_incoming(b'{"ack":"PONG","uptime_s":1}\n')
    assert link.read_message() is None

    link._handle_incoming(b'{"ack":"IR_TRIGGER","sensor":2,"ts":123}\n')
    assert link.read_message() == {
        "ack": "IR_TRIGGER",
        "sensor": 2,
        "ts": 123,
    }


def test_heartbeat_sends_ping_without_reading_uart():
    link = SerialLink(_cfg(), threading.Event())
    fake = _FakeSerial()

    with link._state_lock:
        link._serial = fake
        link._connected = True
        link._next_ping_at = 0.0

    link._service_heartbeat()

    assert fake.writes == [cmd_ping()]
    assert link._awaiting_pong is True
