"""
tests/unit/test_timing_validator.py
Kiểm tra TimingValidator: window computation và is_valid logic.

Chạy: pytest tests/ -v
"""

import pytest
from control.timing_validator import TimingValidator


@pytest.fixture
def cfg():
    return {
        "conveyor": {
            "timing": {
                "ir1_window_ms": [700, 1000],
                "ir2_window_ms": [1200, 1800],
            }
        }
    }


class TestTimingWindow:

    def test_valid_center(self, cfg):
        v = TimingValidator(cfg)
        assert v.is_valid(1, 850) is True

    def test_valid_lower_bound(self, cfg):
        v = TimingValidator(cfg)
        assert v.is_valid(1, 700) is True

    def test_valid_upper_bound(self, cfg):
        v = TimingValidator(cfg)
        assert v.is_valid(1, 1000) is True

    def test_too_early(self, cfg):
        v = TimingValidator(cfg)
        assert v.is_valid(1, 699) is False

    def test_too_late(self, cfg):
        v = TimingValidator(cfg)
        assert v.is_valid(1, 1001) is False

    def test_ir2_valid(self, cfg):
        v = TimingValidator(cfg)
        assert v.is_valid(2, 1500) is True

    def test_ir2_invalid(self, cfg):
        v = TimingValidator(cfg)
        assert v.is_valid(2, 900) is False

    def test_unknown_sensor_defaults_permissive(self, cfg):
        v = TimingValidator(cfg)
        assert v.is_valid(99, 5000) is True  # (0, 9999) fallback

    def test_get_window(self, cfg):
        v = TimingValidator(cfg)
        assert v.window(1) == (700, 1000)
        assert v.window(2) == (1200, 1800)


class TestComputeWindow:

    def test_nominal_calculation(self):
        # 0.25m / 0.3 m/s = 833ms ± 20%  → [666, 1000]
        lo, hi = TimingValidator.compute_window(0.25, 0.30, 20.0)
        assert lo == pytest.approx(666, abs=2)
        assert hi == pytest.approx(1000, abs=2)

    def test_zero_tolerance(self):
        lo, hi = TimingValidator.compute_window(0.30, 0.30, 0.0)
        assert lo == hi

    def test_returns_integers(self):
        lo, hi = TimingValidator.compute_window(0.25, 0.30, 20.0)
        assert isinstance(lo, int)
        assert isinstance(hi, int)