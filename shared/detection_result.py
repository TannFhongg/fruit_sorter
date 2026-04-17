"""
shared/detection_result.py
Cấu trúc dữ liệu dùng chung toàn project.
"""

from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum
import time


class FruitColor(str, Enum):
    GREEN   = "GREEN"
    RED     = "RED"
    YELLOW  = "YELLOW"
    UNKNOWN = "UNKNOWN"


class SortAction(str, Enum):
    SERVO1_LEFT  = "SERVO1_LEFT"
    SERVO1_RIGHT = "SERVO1_RIGHT"
    SERVO2_LEFT  = "SERVO2_LEFT"
    SERVO2_RIGHT = "SERVO2_RIGHT"
    REJECT       = "REJECT"


@dataclass
class DetectionResult:
    """Thread 1 → Thread 2 qua Shared Queue."""
    fruit_color:  FruitColor
    confidence:   float
    timestamp_ms: float    = field(default_factory=lambda: time.monotonic() * 1000)
    frame_id:     int      = 0
    bbox:         tuple    = field(default_factory=tuple)
    action:       SortAction = SortAction.REJECT

    def __repr__(self) -> str:
        return (
            f"DetectionResult({self.fruit_color.value} "
            f"conf={self.confidence:.2f} action={self.action.value})"
        )


@dataclass
class SortEvent:
    """Ghi vào SQLite sau khi servo đã kích."""
    fruit_color:  str
    confidence:   float
    action:       str
    sorted_at_ms: float = field(default_factory=lambda: time.time() * 1000)
    station:      int   = 1
    is_reject:    bool  = False