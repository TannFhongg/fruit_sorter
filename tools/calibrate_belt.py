"""
tools/calibrate_belt.py
=======================
Đo T_travel thực tế giữa Camera → IR1 và Camera → IR2.
Ghi kết quả vào config/hardware_config.yaml.

Cách dùng:
    python tools/calibrate_belt.py
    python tools/calibrate_belt.py --runs 15 --config config/hardware_config.yaml
"""

from __future__ import annotations

import argparse
import statistics
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from config.loader import load_config
from drivers.serial_link import SerialLink
from shared.serial_protocol import parse_response, is_ir_trigger


def measure_sensor(
    sensor_id: int,
    serial: SerialLink,
    runs: int,
) -> list[float]:
    """Tương tác người dùng: đặt vật thể → chờ IR → ghi delta_t."""
    results: list[float] = []
    print(f"\n{'─'*50}")
    print(f"  Đo IR{sensor_id}  ({runs} lần)")
    print(f"{'─'*50}")
    print("  Hướng dẫn: Đặt vật thể ngay trước Camera")
    print("  khi có tín hiệu GO. Đợi IR phát hiện tự động.\n")

    collected = 0
    while collected < runs:
        input(f"  [Lần {collected+1}/{runs}] Nhấn ENTER để bắt đầu...")
        t_start = time.monotonic() * 1000
        print("  >>> GO — đặt vật thể lên băng chuyền ngay!")

        deadline  = time.monotonic() + 12.0
        triggered = False
        while time.monotonic() < deadline:
            raw = serial.read_line()
            if not raw:
                continue
            msg = parse_response(raw)
            if msg and is_ir_trigger(msg) and msg.get("sensor") == sensor_id:
                delta_ms = time.monotonic() * 1000 - t_start
                results.append(delta_ms)
                print(f"  ✓  IR{sensor_id} triggered — delta_t = {delta_ms:.0f}ms")
                triggered = True
                break

        if not triggered:
            print(f"  ✗  Timeout (12s) — thử lại lần này")
        else:
            collected += 1

    return results


def compute_window(
    times_ms: list[float],
    tolerance_pct: float = 20.0,
) -> tuple[int, int]:
    mean  = statistics.mean(times_ms)
    stdev = statistics.stdev(times_ms) if len(times_ms) > 1 else 0
    lo = max(0, int((mean - 2 * stdev) * (1 - tolerance_pct / 100)))
    hi = int((mean + 2 * stdev) * (1 + tolerance_pct / 100))
    return lo, hi


def main() -> None:
    parser = argparse.ArgumentParser(description="Belt calibration tool")
    parser.add_argument("--runs",   type=int, default=10)
    parser.add_argument("--config", default="config/hardware_config.yaml")
    parser.add_argument("--sensors", nargs="+", type=int, default=[1, 2],
                        help="Which IR sensors to calibrate (default: 1 2)")
    args = parser.parse_args()

    cfg  = load_config(args.config)
    stop = threading.Event()

    print("\n" + "="*52)
    print("  FruitSorter — Belt Calibration Tool")
    print("="*52)
    print(f"Config : {args.config}")
    print(f"Runs   : {args.runs} per sensor")
    print(f"Sensors: IR{args.sensors}")

    serial = SerialLink(cfg, stop)
    serial.start()

    print("\nKết nối Arduino...", end=" ", flush=True)
    for _ in range(10):
        time.sleep(0.5)
        if serial.is_connected:
            break
    if not serial.is_connected:
        print("THẤT BẠI\nKiểm tra: port serial trong hardware_config.yaml")
        stop.set()
        sys.exit(1)
    print("OK ✓")

    # ── Đo từng sensor ─────────────────────────────────────────────
    all_windows: dict[str, list[int]] = {}

    for sid in args.sensors:
        times = measure_sensor(sid, serial, args.runs)
        if not times:
            print(f"Không có dữ liệu cho IR{sid}")
            continue
        lo, hi = compute_window(times)
        mean   = statistics.mean(times)
        stdev  = statistics.stdev(times) if len(times) > 1 else 0

        print(f"\n  IR{sid} kết quả:")
        print(f"    Trung bình  : {mean:.0f}ms")
        print(f"    Độ lệch chuẩn: {stdev:.0f}ms")
        print(f"    Cửa sổ đề xuất: [{lo}, {hi}]ms")
        all_windows[f"ir{sid}_window_ms"] = [lo, hi]

    # ── Ghi vào config ──────────────────────────────────────────────
    if not all_windows:
        print("\nKhông có kết quả để ghi.")
        stop.set()
        return

    print(f"\n{'─'*52}")
    ans = input("Ghi kết quả vào hardware_config.yaml? [y/N]: ").strip().lower()
    if ans == "y":
        import yaml
        with open(args.config, "r", encoding="utf-8") as f:
            raw_cfg = yaml.safe_load(f)
        timing = raw_cfg.setdefault("conveyor", {}).setdefault("timing", {})
        timing.update(all_windows)
        with open(args.config, "w", encoding="utf-8") as f:
            yaml.dump(raw_cfg, f, default_flow_style=False, allow_unicode=True)
        print(f"✓  Đã ghi vào {args.config}")
    else:
        print("Bỏ qua — không ghi file.")

    stop.set()
    print("\nCalibration hoàn tất.\n")


if __name__ == "__main__":
    main()