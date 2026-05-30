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

from shared.serial_protocol import cmd_ping, cmd_status, cmd_reset, parse_response


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port",    default="/dev/ttyUSB0")
    parser.add_argument("--baud",    type=int, default=115200)
    parser.add_argument("--timeout", type=float, default=2.0)
    args = parser.parse_args()

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

    def send_and_print(label: str, data: bytes) -> None:
        print(f"\n[{label}] → {data.decode().strip()}")
        ser.write(data)
        resp = ser.readline()
        if resp:
            msg = parse_response(resp)
            print(f"[{label}] ← {msg}")
        else:
            print(f"[{label}] ← timeout")

    send_and_print("PING",   cmd_ping())
    send_and_print("STATUS", cmd_status())
    send_and_print("RESET",  cmd_reset())
    send_and_print("PING",   cmd_ping())   # verify after reset

    print("\n✓ Test hoàn tất")
    ser.close()


if __name__ == "__main__":
    main()