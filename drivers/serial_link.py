"""
drivers/serial_link.py
UART Serial link — Raspberry Pi (Master) ↔ Arduino (Slave).
Tự động reconnect, heartbeat, thread-safe send/receive.

Tương đương: master/utils/serial_manager.py (cũ)
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Optional

from shared.serial_protocol import parse_response, cmd_ping, is_pong

log = logging.getLogger(__name__)


class SerialLink(threading.Thread):
    """
    Daemon thread duy trì kết nối Serial với Arduino.
    Expose: send(bytes), read_line() → bytes | None, is_connected
    """

    def __init__(self, cfg: dict, stop_event: threading.Event):
        super().__init__(name="SerialLink", daemon=True)
        ser_cfg         = cfg["arduino"]["serial"]
        hb_cfg          = cfg["arduino"]["heartbeat"]
        self._port      = ser_cfg["port"]
        self._baud      = ser_cfg["baudrate"]
        self._timeout   = ser_cfg["timeout_s"]
        self._delay     = ser_cfg["reconnect_delay_s"]
        self._max_retry = ser_cfg.get("reconnect_max", 999)
        self._hb_ivl    = hb_cfg["interval_s"]
        self._hb_max    = hb_cfg["max_missed"]
        self._stop      = stop_event
        self._serial    = None
        self._lock      = threading.Lock()
        self._connected = False
        self._missed    = 0

    # ── Public API ─────────────────────────────────────────────────────────

    @property
    def is_connected(self) -> bool:
        return self._connected

    def send(self, data: bytes) -> bool:
        with self._lock:
            if not self._connected or self._serial is None:
                return False
            try:
                self._serial.write(data)
                return True
            except Exception as e:
                log.error(f"Serial write error: {e}")
                self._connected = False
                return False

    def read_line(self) -> Optional[bytes]:
        with self._lock:
            if not self._connected or self._serial is None:
                return None
            try:
                return self._serial.readline()
            except Exception as e:
                log.error(f"Serial read error: {e}")
                self._connected = False
                return None

    # ── Thread body ────────────────────────────────────────────────────────

    def run(self) -> None:
        retries = 0
        while not self._stop.is_set():
            if not self._connected:
                self._try_connect()
                retries += 1
                if retries > self._max_retry:
                    log.critical("Max serial reconnect attempts — aborting")
                    self._stop.set()
                    return
                time.sleep(self._delay)
            else:
                time.sleep(self._hb_ivl)
                self._heartbeat()
        self._cleanup()

    def _try_connect(self) -> None:
        try:
            import serial
            s = serial.Serial(
                port=self._port, baudrate=self._baud, timeout=self._timeout
            )
            with self._lock:
                self._serial    = s
                self._connected = True
                self._missed    = 0
            log.info(f"Arduino connected: {self._port} @ {self._baud}")
        except Exception as e:
            log.warning(f"Connect failed: {e}")

    def _heartbeat(self) -> None:
        self.send(cmd_ping())
        raw = self.read_line()
        if raw:
            msg = parse_response(raw)
            if msg and is_pong(msg):
                self._missed = 0
                return
        self._missed += 1
        log.warning(f"Heartbeat miss #{self._missed}/{self._hb_max}")
        if self._missed >= self._hb_max:
            log.error("Arduino not responding — reconnecting")
            self._connected = False

    def _cleanup(self) -> None:
        with self._lock:
            if self._serial:
                self._serial.close()
        log.info("SerialLink closed")