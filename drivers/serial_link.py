"""
drivers/serial_link.py
UART Serial link — Raspberry Pi (Master) ↔ Arduino (Slave).
Tự động reconnect, heartbeat, thread-safe send, single-consumer receive.

Production invariant:
    SerialLink is the only thread that calls serial.readline().
    It parses every inbound UART line, consumes PONG for heartbeat, and
    queues all other messages for application threads via read_message().

This prevents heartbeat from swallowing IR_TRIGGER and prevents
SortController from swallowing PONG, which could otherwise cause missed
fruit or false reconnects.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from typing import Optional

from shared.serial_protocol import parse_response, cmd_ping, is_pong

log = logging.getLogger(__name__)

HEARTBEAT_READ_TIMEOUT_S = 0.2
SERIAL_READ_POLL_TIMEOUT_S = 0.05
SERIAL_EVENT_QUEUE_MAXLEN = 100


class SerialLink(threading.Thread):
    """
    Daemon thread duy trì kết nối Serial với Arduino.
    Expose: send(bytes), read_message() -> dict | None, is_connected

    Khi hết reconnect_max lần thử: thread tự dừng (log CRITICAL)
    nhưng KHÔNG set stop_event — các thread khác (camera, web) vẫn chạy.

    Lock hierarchy:
        _state_lock : protects _connected, _serial reference
        _tx_lock    : protects serial.write()
        _rx_lock    : protects serial.readline()
        _msg_lock   : protects parsed message queue
    """

    def __init__(self, cfg: dict, stop_event: threading.Event):
        super().__init__(name="SerialLink", daemon=True)
        ser_cfg         = cfg["arduino"]["serial"]
        hb_cfg          = cfg["arduino"]["heartbeat"]
        self._port      = ser_cfg["port"]
        self._baud      = ser_cfg["baudrate"]
        self._timeout   = ser_cfg["timeout_s"]
        self._poll_timeout = min(self._timeout, SERIAL_READ_POLL_TIMEOUT_S)
        self._delay     = ser_cfg["reconnect_delay_s"]
        self._max_retry = ser_cfg.get("reconnect_max", 999)
        self._hb_ivl    = hb_cfg["interval_s"]
        self._hb_max    = hb_cfg["max_missed"]
        self._stop      = stop_event

        self._serial    = None
        self._connected = False
        self._missed    = 0
        self._awaiting_pong = False
        self._ping_sent_at  = 0.0
        self._next_ping_at  = 0.0
        self._rx_queue_maxlen = int(
            cfg.get("system", {}).get(
                "serial_event_queue_maxlen",
                SERIAL_EVENT_QUEUE_MAXLEN,
            )
        )
        self._rx_queue = deque(maxlen=self._rx_queue_maxlen)

        # ── Independent locks (see module docstring) ──────────────────────
        self._state_lock = threading.Lock()   # connect/disconnect state
        self._tx_lock    = threading.Lock()   # serial.write()
        self._rx_lock    = threading.Lock()   # serial.readline()
        self._msg_lock   = threading.Lock()   # parsed inbound messages

    # ── Public API ─────────────────────────────────────────────────────────

    @property
    def is_connected(self) -> bool:
        with self._state_lock:
            return self._connected

    def send(self, data: bytes) -> bool:
        """Thread-safe write. Returns False if not connected or write fails."""
        # Check connection state first (cheap, brief lock)
        with self._state_lock:
            if not self._connected or self._serial is None:
                return False
            ser = self._serial  # local ref; serial object itself is thread-safe for writes

        # Acquire TX lock only for the duration of the write syscall
        with self._tx_lock:
            try:
                ser.write(data)
                return True
            except Exception as e:
                log.error("Serial write error: %s", e)
                with self._state_lock:
                    self._connected = False
                return False

    def read_line(self) -> Optional[bytes]:
        """
        Legacy raw readline. Do not use while this thread is running.
        Production code must use read_message() so SerialLink remains the
        only UART reader and heartbeat/IR messages cannot be stolen.
        """
        return self._read_raw_line()

    def read_message(self) -> Optional[dict]:
        """Return the oldest parsed non-PONG UART message, if available."""
        with self._msg_lock:
            if not self._rx_queue:
                return None
            return self._rx_queue.popleft()

    def _read_raw_line(self) -> Optional[bytes]:
        with self._state_lock:
            if not self._connected or self._serial is None:
                return None
            ser = self._serial

        with self._rx_lock:
            try:
                return ser.readline()
            except Exception as e:
                log.error("Serial read error: %s", e)
                with self._state_lock:
                    self._connected = False
                return None

    # ── Thread body ────────────────────────────────────────────────────────

    def run(self) -> None:
        failed_reconnects = 0
        while not self._stop.is_set():
            if not self.is_connected:
                if failed_reconnects >= self._max_retry:
                    log.critical(
                        "Max serial reconnect attempts — Arduino unavailable. "
                        "SerialLink thread exiting (camera & web still running)."
                    )
                    self._cleanup()
                    return
                if self._try_connect():
                    failed_reconnects = 0
                    continue
                failed_reconnects += 1
                time.sleep(self._delay)
            else:
                raw = self._read_raw_line()
                if raw:
                    self._handle_incoming(raw)
                self._service_heartbeat()
                if not raw:
                    time.sleep(0.001)
        self._cleanup()

    def _try_connect(self) -> bool:
        try:
            import serial
            s = serial.Serial(
                port=self._port, baudrate=self._baud, timeout=self._poll_timeout
            )
            with self._state_lock:
                if self._serial:
                    try:
                        self._serial.close()
                    except Exception:
                        pass
                self._serial    = s
                self._connected = True
                self._missed    = 0
                self._awaiting_pong = False
                self._next_ping_at  = time.monotonic() + self._hb_ivl
            log.info(
                "Arduino connected: %s @ %d (read_timeout=%.3fs)",
                self._port, self._baud, self._poll_timeout,
            )
            return True
        except Exception as e:
            log.warning("Connect failed: %s", e)
            return False

    def _handle_incoming(self, raw: bytes) -> None:
        msg = parse_response(raw)
        if not msg:
            log.warning("Ignoring invalid serial line: %r", raw)
            return

        if is_pong(msg):
            self._missed = 0
            self._awaiting_pong = False
            return

        with self._msg_lock:
            if len(self._rx_queue) >= self._rx_queue_maxlen:
                dropped = self._rx_queue.popleft()
                log.error("Serial RX queue full; dropping oldest message: %s", dropped)
            self._rx_queue.append(msg)

    def _service_heartbeat(self) -> None:
        now = time.monotonic()

        if self._awaiting_pong:
            if now - self._ping_sent_at < HEARTBEAT_READ_TIMEOUT_S:
                return

            self._awaiting_pong = False
            self._missed += 1
            log.warning("Heartbeat miss #%d/%d", self._missed, self._hb_max)
            if self._missed >= self._hb_max:
                log.error("Arduino not responding — reconnecting")
                with self._state_lock:
                    self._connected = False
            return

        if now < self._next_ping_at:
            return

        if self.send(cmd_ping()):
            self._awaiting_pong = True
            self._ping_sent_at  = now
            self._next_ping_at  = now + self._hb_ivl

    def _cleanup(self) -> None:
        with self._state_lock:
            if self._serial:
                self._serial.close()
                self._serial = None
            self._connected = False
        log.info("SerialLink closed")
