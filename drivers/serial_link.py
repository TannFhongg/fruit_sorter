"""
drivers/serial_link.py
UART Serial link — Raspberry Pi (Master) ↔ Arduino (Slave).
Tự động reconnect, heartbeat, thread-safe send/receive.

=======================================================================
Bug Fix — Lock contention causing T2 stall (1s every 5s)
=======================================================================
PROBLEM (original code):
    send()     → acquires self._lock
    read_line() → acquires self._lock

    _heartbeat() calls send() then read_line() sequentially.
    read_line() holds the lock for the full serial timeout (1.0s)
    while waiting for the Arduino PONG response.

    SortController (T2) calls serial.read_line() in its tight loop
    to receive IR_TRIGGER events. Because both use the same lock,
    T2 is blocked for up to 1 second every 5 seconds (heartbeat interval).

    At conveyor speed 0.3 m/s with fruit spacing ~15 cm, inter-fruit
    interval ≈ 500ms. A 1-second stall means T2 can miss IR events
    entirely during the heartbeat window.

FIXED ARCHITECTURE — separate TX and RX locks:
    self._tx_lock  : guards serial.write() only
    self._rx_lock  : guards serial.readline() only

    TX and RX on a UART are independent at the hardware level.
    Two threads can safely write and read simultaneously on pyserial's
    Serial object as long as they don't call the same method concurrently.
    Separating the locks allows T2 to call read_line() while heartbeat
    calls send() without any contention.

    The only remaining shared state is self._connected and self._serial.
    These are guarded by self._state_lock (a third, rarely-contended lock
    used only for connect/disconnect transitions, not for I/O).

Lock acquisition order (to prevent deadlock):
    Any code that needs multiple locks must acquire in this order:
        1. _state_lock  (connect/disconnect checks)
        2. _tx_lock     (write)
        3. _rx_lock     (read)
    In practice, no code path acquires more than one lock at a time.

Heartbeat read:
    After send(PING), _heartbeat() calls read_line() which acquires
    _rx_lock. T2 also calls read_line(). They can contend, but only
    for the duration of a single readline() call (microseconds when
    data is available, or timeout_s when not). The heartbeat timeout
    is 1.0s — but T2 will only be blocked for the fraction of that
    second that the hardware actually takes to respond (typically <5ms
    on a local UART). The worst case (Arduino not responding) is still
    1s, but this is now an exceptional condition, not the normal path.

    To further protect T2 during the abnormal case, heartbeat uses
    a shorter dedicated timeout (HEARTBEAT_READ_TIMEOUT_S = 0.2s)
    separate from the general I/O timeout. If no PONG arrives within
    200ms, it is counted as a miss without holding _rx_lock for 1s.
=======================================================================
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Optional

from shared.serial_protocol import parse_response, cmd_ping, is_pong

log = logging.getLogger(__name__)

# Heartbeat reads use a short timeout so _rx_lock is released quickly
# even when the Arduino is unresponsive.
HEARTBEAT_READ_TIMEOUT_S = 0.2


class SerialLink(threading.Thread):
    """
    Daemon thread duy trì kết nối Serial với Arduino.
    Expose: send(bytes), read_line() → bytes | None, is_connected

    Khi hết reconnect_max lần thử: thread tự dừng (log CRITICAL)
    nhưng KHÔNG set stop_event — các thread khác (camera, web) vẫn chạy.

    Lock hierarchy (never held simultaneously):
        _state_lock : protects _connected, _serial reference
        _tx_lock    : protects serial.write()
        _rx_lock    : protects serial.readline()
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
        self._connected = False
        self._missed    = 0

        # ── Three independent locks (see module docstring) ────────────────
        self._state_lock = threading.Lock()   # connect/disconnect state
        self._tx_lock    = threading.Lock()   # serial.write()
        self._rx_lock    = threading.Lock()   # serial.readline()

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
        Thread-safe readline. Returns None if not connected or read fails.

        Holds _rx_lock for the duration of readline(), which blocks for
        up to self._timeout seconds. Callers in tight loops (T2) should
        be aware that this call can block. The lock separation ensures
        this does NOT block concurrent send() calls.
        """
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
        retries = 0
        while not self._stop.is_set():
            if not self.is_connected:
                self._try_connect()
                retries += 1
                if retries > self._max_retry:
                    log.critical(
                        "Max serial reconnect attempts — Arduino unavailable. "
                        "SerialLink thread exiting (camera & web still running)."
                    )
                    self._cleanup()
                    return
                time.sleep(self._delay)
            else:
                retries = 0
                time.sleep(self._hb_ivl)
                self._heartbeat()
        self._cleanup()

    def _try_connect(self) -> None:
        try:
            import serial
            s = serial.Serial(
                port=self._port, baudrate=self._baud, timeout=self._timeout
            )
            with self._state_lock:
                self._serial    = s
                self._connected = True
                self._missed    = 0
            log.info("Arduino connected: %s @ %d", self._port, self._baud)
        except Exception as e:
            log.warning("Connect failed: %s", e)

    def _heartbeat(self) -> None:
        """
        Send PING and wait for PONG.

        Uses a short dedicated timeout (HEARTBEAT_READ_TIMEOUT_S) so that
        _rx_lock is held for at most ~200ms even when Arduino is unresponsive,
        rather than the full self._timeout (1.0s).

        Implementation: temporarily patch the serial timeout for this read,
        then restore it. This is safe because _rx_lock serialises all reads —
        no other thread can read concurrently, so the timeout change is atomic
        with respect to other readers.
        """
        # Send PING (uses _tx_lock internally)
        ok = self.send(cmd_ping())
        if not ok:
            return

        # Read PONG with short timeout to limit _rx_lock hold time
        with self._state_lock:
            if not self._connected or self._serial is None:
                return
            ser = self._serial

        with self._rx_lock:
            try:
                old_timeout     = ser.timeout
                ser.timeout     = HEARTBEAT_READ_TIMEOUT_S
                raw             = ser.readline()
                ser.timeout     = old_timeout   # restore for T2 reads
            except Exception as e:
                log.error("Heartbeat read error: %s", e)
                with self._state_lock:
                    self._connected = False
                return

        if raw:
            msg = parse_response(raw)
            if msg and is_pong(msg):
                self._missed = 0
                return

        self._missed += 1
        log.warning("Heartbeat miss #%d/%d", self._missed, self._hb_max)
        if self._missed >= self._hb_max:
            log.error("Arduino not responding — reconnecting")
            with self._state_lock:
                self._connected = False

    def _cleanup(self) -> None:
        with self._state_lock:
            if self._serial:
                self._serial.close()
        log.info("SerialLink closed")