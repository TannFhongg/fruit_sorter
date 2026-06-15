"""
tools/test_serial.py
====================
Kiểm tra kết nối UART với Arduino Slave một cách thủ công.
Gửi PING, STATUS, RESET và in phản hồi.

Cách dùng:
    python tools/test_serial.py
    python tools/test_serial.py --port /dev/ttyACM0
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from config.loader import load_config
from shared.serial_protocol import cmd_ping, cmd_status, cmd_reset, parse_response


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port",    default="/dev/ttyUSB0")
    parser.add_argument("--baud",    type=int, default=115200)
    parser.add_argument("--timeout", type=float, default=2.0)
    parser.add_argument("--config",  default="config/hardware_config.yaml")
    args = parser.parse_args()

    cfg = load_config(args.config)
    servos = cfg.get("hardware", {}).get("servos", {})
    servo1 = servos.get("servo1", {})
    servo2 = servos.get("servo2", {})
    reset_cmd = cmd_reset(
        {
            1: servo1.get("angle_home", 0),
            2: servo2.get("angle_home", 0),
        },
        angle_max=servo1.get("angle_max", 270),
        pulse_min_us=servo1.get("pulse_min_us", 500),
        pulse_max_us=servo1.get("pulse_max_us", 2500),
    )

    try:
        import serial
    except ImportError:
        print("pip install pyserial")
        sys.exit(1)

    print(f"Kết nối {args.port} @ {args.baud}...")
    try:
        ser = serial.Serial(args.port, args.baud, timeout=args.timeout)
    except Exception as e:
        print(f"Lỗi: {e}")
        sys.exit(1)

    time.sleep(2)  # chờ Arduino reset

    def read_line(timeout_s: float) -> dict | bytes | None:
        old_timeout = ser.timeout
        ser.timeout = timeout_s
        try:
            raw = ser.readline()
        finally:
            ser.timeout = old_timeout
        if not raw:
            return None
        return parse_response(raw) or raw

    def drain_pending(label: str, quiet_s: float = 0.2, timeout_s: float = 2.0) -> int:
        drained = 0
        deadline = time.monotonic() + timeout_s
        quiet_deadline = time.monotonic() + quiet_s

        while time.monotonic() < deadline:
            msg = read_line(min(0.05, quiet_s))
            if msg is not None:
                drained += 1
                quiet_deadline = time.monotonic() + quiet_s
                print(f"[{label}] async/stale ← {msg}")
                continue

            if time.monotonic() >= quiet_deadline:
                break

        return drained

    def send_and_print(label: str, data: bytes, expected_ack: str) -> None:
        print(f"\n[{label}] → {data.decode().strip()}")
        ser.write(data)

        deadline = time.monotonic() + args.timeout
        while time.monotonic() < deadline:
            msg = read_line(0.05)
            if msg is None:
                continue
            if isinstance(msg, dict) and msg.get("ack") == expected_ack:
                print(f"[{label}] ← {msg}")
                return
            print(f"[{label}] async/stale ← {msg}")

        print(f"[{label}] ← timeout waiting for ack={expected_ack}")

    drained = drain_pending("STARTUP")
    if drained:
        print(f"\nĐã xả {drained} dòng serial cũ trước khi test.")

    send_and_print("PING",   cmd_ping(),   "PONG")
    send_and_print("STATUS", cmd_status(), "STATUS")
    send_and_print("RESET",  reset_cmd,    "RESET_DONE")
    send_and_print("PING",   cmd_ping(),   "PONG")   # verify after reset

    print("\n✓ Test hoàn tất")
    ser.close()


if __name__ == "__main__":
    main()
