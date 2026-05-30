"""
database/db_queries.py
Read-only SQL queries cho Flask API endpoints.
"""

from __future__ import annotations
import sqlite3
from datetime import datetime, timedelta


def _conn(path: str) -> sqlite3.Connection:
    c = sqlite3.connect(path, check_same_thread=False)
    c.row_factory = sqlite3.Row
    return c


def get_today_stats(path: str) -> dict:
    today = datetime.now().strftime("%Y-%m-%d")
    try:
        with _conn(path) as c:
            row = c.execute(
                "SELECT green,red,yellow,rejects,total FROM daily_stats WHERE date=?",
                (today,)
            ).fetchone()
        return dict(row) if row else {}
    except Exception:
        return {}


def get_history(path: str, days: int = 7) -> list[dict]:
    since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    try:
        with _conn(path) as c:
            rows = c.execute(
                "SELECT date,green,red,yellow,rejects,total FROM daily_stats "
                "WHERE date>=? ORDER BY date ASC", (since,)
            ).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []


def get_recent_events(path: str, limit: int = 50) -> list[dict]:
    try:
        with _conn(path) as c:
            rows = c.execute(
                "SELECT id,fruit_color,confidence,action,station,is_reject,sorted_at "
                "FROM sort_events ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []


def get_hourly_breakdown(path: str) -> list[dict]:
    today = datetime.now().strftime("%Y-%m-%d")
    try:
        with _conn(path) as c:
            rows = c.execute("""
                SELECT
                  CAST(strftime('%H',datetime(sorted_at/1000,'unixepoch','localtime')) AS INT) AS hour,
                  fruit_color, COUNT(*) AS count
                FROM sort_events
                WHERE date(datetime(sorted_at/1000,'unixepoch','localtime'))=?
                  AND is_reject=0
                GROUP BY hour,fruit_color ORDER BY hour
            """, (today,)).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []