"""
control/timing_validator.py
Tính và kiểm tra cửa sổ thời gian hợp lệ cho từng IR sensor.
Tách ra module riêng để dễ unit test.
"""

from __future__ import annotations
import math


class TimingValidator:
    """
    Xác nhận delta_t giữa Camera detect và IR trigger.

    Công thức:
        T_nominal = distance_m / speed_m_s  (giây)
        T_window  = [T_nominal × (1 - tol), T_nominal × (1 + tol)]
    """

    def __init__(self, cfg: dict):
        timing = cfg["conveyor"]["timing"]
        self._windows: dict[int, tuple[float, float]] = {
            1: tuple(timing.get("ir1_window_ms", [700,  1000])),
            2: tuple(timing.get("ir2_window_ms", [1200, 1800])),
        }

    def is_valid(self, sensor_id: int, delta_ms: float) -> bool:
        lo, hi = self._windows.get(sensor_id, (0, 9999))
        return lo <= delta_ms <= hi

    def window(self, sensor_id: int) -> tuple[float, float]:
        return self._windows.get(sensor_id, (0, 9999))

    @staticmethod
    def compute_window(
        distance_m: float,
        speed_m_s: float,
        tolerance_pct: float = 20.0,
    ) -> tuple[int, int]:
        """Tính window từ tham số vật lý."""
        t_ms  = (distance_m / speed_m_s) * 1000
        tol   = tolerance_pct / 100
        return (
            math.floor(t_ms * (1 - tol)),
            math.ceil( t_ms * (1 + tol)),
        )