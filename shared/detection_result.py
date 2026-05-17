"""
shared/detection_result.py
Cấu trúc dữ liệu dùng chung toàn project.

=======================================================================
Bug Fix — time.monotonic() vs time.time() inconsistency
=======================================================================
PROBLEM: Two different clocks are used across the codebase:

    DetectionResult.timestamp_ms  → time.monotonic() * 1000
    SortEvent.sorted_at_ms        → time.time()      * 1000

time.monotonic() is a machine uptime counter: suitable for measuring
intervals (delta_ms in SortController), but NOT a valid Unix timestamp.
time.time() is wall-clock: suitable for SQLite storage and SQL date
functions (strftime, unixepoch).

If code ever passes DetectionResult.timestamp_ms to a place expecting
a Unix timestamp (e.g. SortEvent.sorted_at_ms), the stored value will
be the system uptime in milliseconds — typically a number like 123456789
which corresponds to sometime around 1971 in the Unix epoch.
db_queries.py would then return wrong dates from:
    datetime(sorted_at/1000, 'unixepoch', 'localtime')

The current code does NOT mix them up, but there is no guard to prevent
a future regression. The field names are not self-documenting enough:
both are named "*_ms" and both are floats.

FIXED PATTERN — type aliases + assertion in SortEvent:

1. Two named type aliases make the intended clock explicit at call sites:
       MonotonicMs = float   # time.monotonic() * 1000 — for intervals only
       WallClockMs = float   # time.time()      * 1000 — for DB storage

2. SortEvent.__post_init__ asserts that sorted_at_ms looks like a
   plausible Unix timestamp (> year 2020 in ms). This catches the
   mistake at runtime the moment the wrong value is supplied.

3. DetectionResult.timestamp_ms is typed MonotonicMs.
   SortEvent.sorted_at_ms is typed WallClockMs.
   Any future use of timestamp_ms where WallClockMs is expected will
   produce a type-checker warning (mypy / pyright).

The assertion threshold is Jan 1 2020 = 1577836800000 ms.
Monotonic values on a freshly booted RPi4 are typically <86400000 ms
(less than 1 day of uptime), so the assertion reliably rejects them.
=======================================================================
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum

# ── Clock type aliases — use these in type annotations everywhere ──────────
#
# MonotonicMs: value from time.monotonic() * 1000.
#   Properties: always increasing, no wall-clock meaning, safe for intervals.
#   Use for: DetectionResult.timestamp_ms, timing deltas.
#
# WallClockMs: value from time.time() * 1000.
#   Properties: Unix epoch milliseconds, correct for SQLite/datetime.
#   Use for: SortEvent.sorted_at_ms, any DB timestamp column.
#
MonotonicMs = float   # time.monotonic() * 1000
WallClockMs = float   # time.time()      * 1000

# Minimum plausible wall-clock timestamp (ms): 2020-01-01T00:00:00Z
# Monotonic values on a live RPi are << this; detected immediately.
_MIN_WALLCLOCK_MS: WallClockMs = 1_577_836_800_000.0


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
    """
    Thread 1 → Thread 2 qua Shared Queue.

    timestamp_ms is a MonotonicMs value (time.monotonic() * 1000).
    It is used ONLY for computing delta_ms in SortController.
    It must NEVER be stored in the database or used as a wall-clock time.
    Use SortEvent.sorted_at_ms (WallClockMs) for storage.
    """
    fruit_color:  FruitColor
    confidence:   float
    # MonotonicMs — suitable ONLY for interval measurement, not DB storage
    timestamp_ms: MonotonicMs = field(
        default_factory=lambda: time.monotonic() * 1000
    )
    frame_id:     int         = 0
    bbox:         tuple       = field(default_factory=tuple)
    action:       SortAction  = SortAction.REJECT

    def __repr__(self) -> str:
        return (
            f"DetectionResult({self.fruit_color.value} "
            f"conf={self.confidence:.2f} action={self.action.value})"
        )


@dataclass
class SortEvent:
    """
    Ghi vào SQLite sau khi servo đã kích.

    sorted_at_ms is a WallClockMs value (time.time() * 1000).
    This is a valid Unix timestamp in milliseconds, suitable for
    SQLite's datetime(sorted_at/1000, 'unixepoch', 'localtime').

    __post_init__ asserts the value is a plausible wall-clock timestamp
    to catch the common mistake of passing DetectionResult.timestamp_ms
    (which is MonotonicMs and would produce dates around 1970).
    """
    fruit_color:  str
    confidence:   float
    action:       str
    # WallClockMs — must be time.time() * 1000, NOT time.monotonic() * 1000
    sorted_at_ms: WallClockMs = field(
        default_factory=lambda: time.time() * 1000
    )
    station:      int         = 1
    is_reject:    bool        = False

    def __post_init__(self) -> None:
        # Guard against passing MonotonicMs (uptime) as a wall-clock value.
        # On a freshly booted RPi4, monotonic() < 86400s = 86400000ms,
        # which is far below _MIN_WALLCLOCK_MS (year 2020).
        if self.sorted_at_ms < _MIN_WALLCLOCK_MS:
            raise ValueError(
                f"SortEvent.sorted_at_ms={self.sorted_at_ms:.0f} looks like "
                f"a monotonic uptime value, not a Unix wall-clock timestamp. "
                f"Use time.time() * 1000, not time.monotonic() * 1000. "
                f"Expected >= {_MIN_WALLCLOCK_MS:.0f} (year 2020)."
            )