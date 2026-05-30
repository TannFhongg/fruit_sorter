"""
config/loader.py
Đọc hardware_config.yaml một lần, cache suốt vòng đời process.
Tự tính ir_window_ms từ speed + distance nếu chưa ghi đè.
"""

from __future__ import annotations
import math
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

DEFAULT_CONFIG = Path(__file__).parent / "hardware_config.yaml"


@lru_cache(maxsize=1)
def load_config(path: str = str(DEFAULT_CONFIG)) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    _compute_timing_windows(cfg)
    return cfg


def get(key_path: str, default: Any = None) -> Any:
    """Truy xuất bằng dot-notation. Ví dụ: get('model.thresholds.confidence')"""
    parts = key_path.split(".")
    node  = load_config()
    for p in parts:
        if not isinstance(node, dict):
            return default
        node = node.get(p, default)
    return node


def _compute_timing_windows(cfg: dict) -> None:
    conv  = cfg.get("conveyor", {})
    speed = conv.get("speed_m_s", 0.3)
    tol   = conv.get("timing", {}).get("tolerance_pct", 20) / 100
    timing = conv.setdefault("timing", {})

    for key, dist_key in (
        ("ir1_window_ms", "camera_to_ir1_m"),
        ("ir2_window_ms", "camera_to_ir2_m"),
    ):
        if key not in timing:
            dist = conv.get(dist_key)
            if dist and speed > 0:
                t_ms = (dist / speed) * 1000
                timing[key] = [
                    math.floor(t_ms * (1 - tol)),
                    math.ceil(t_ms  * (1 + tol)),
                ]