from __future__ import annotations

import threading
from collections import deque
from datetime import datetime

from database.db_writer import DatabaseWriter
from shared.detection_result import SortEvent


def _cfg(db_path) -> dict:
    return {
        "database": {
            "path": str(db_path),
            "wal_mode": False,
            "cache_kb": 1024,
            "write_queue": {
                "flush_interval_s": 1.0,
                "flush_batch_size": 10,
            },
        }
    }


def _ts_ms(year: int, month: int, day: int, hour: int = 12) -> float:
    return datetime(year, month, day, hour, 0, 0).timestamp() * 1000


def test_daily_stats_use_event_date_for_cross_midnight_batch(tmp_path):
    writer = DatabaseWriter(_cfg(tmp_path / "sorter.db"), deque(), threading.Event())
    writer._conn = writer._connect()
    try:
        writer._write_batch([
            SortEvent(
                fruit_color="GREEN",
                confidence=0.91,
                action="SERVO1_FIRE",
                sorted_at_ms=_ts_ms(2026, 1, 1, 23),
                is_reject=False,
            ),
            SortEvent(
                fruit_color="RED",
                confidence=0.88,
                action="REJECT",
                sorted_at_ms=_ts_ms(2026, 1, 1, 23),
                is_reject=True,
            ),
            SortEvent(
                fruit_color="RED",
                confidence=0.93,
                action="SERVO2_FIRE",
                sorted_at_ms=_ts_ms(2026, 1, 2, 1),
                is_reject=False,
            ),
        ])

        rows = writer._conn.execute(
            "SELECT date,green,red,yellow,rejects,total "
            "FROM daily_stats ORDER BY date"
        ).fetchall()
    finally:
        writer._conn.close()

    assert rows == [
        ("2026-01-01", 1, 0, 0, 1, 2),
        ("2026-01-02", 0, 1, 0, 0, 1),
    ]
