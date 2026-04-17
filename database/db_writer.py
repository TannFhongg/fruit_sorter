"""
database/db_writer.py
Background thread: gom batch SortEvent từ write_queue → SQLite WAL.
Tương đương: master/database/writer.py (cũ)
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)

_SQL_CREATE_EVENTS = """
CREATE TABLE IF NOT EXISTS sort_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    fruit_color TEXT    NOT NULL,
    confidence  REAL    NOT NULL,
    action      TEXT    NOT NULL,
    station     INTEGER NOT NULL DEFAULT 1,
    is_reject   INTEGER NOT NULL DEFAULT 0,
    sorted_at   REAL    NOT NULL
);"""

_SQL_CREATE_STATS = """
CREATE TABLE IF NOT EXISTS daily_stats (
    date    TEXT PRIMARY KEY,
    green   INTEGER DEFAULT 0,
    red     INTEGER DEFAULT 0,
    yellow  INTEGER DEFAULT 0,
    rejects INTEGER DEFAULT 0,
    total   INTEGER DEFAULT 0
);"""

_SQL_INSERT = """
INSERT INTO sort_events
    (fruit_color, confidence, action, station, is_reject, sorted_at)
VALUES (?,?,?,?,?,?)"""

_SQL_UPSERT = """
INSERT INTO daily_stats (date, green, red, yellow, rejects, total)
VALUES (?,?,?,?,?,?)
ON CONFLICT(date) DO UPDATE SET
    green   = green   + excluded.green,
    red     = red     + excluded.red,
    yellow  = yellow  + excluded.yellow,
    rejects = rejects + excluded.rejects,
    total   = total   + excluded.total"""


class DatabaseWriter(threading.Thread):

    def __init__(self, cfg: dict, write_queue: deque, stop_event: threading.Event, **kw):
        super().__init__(**kw)
        db_cfg           = cfg["database"]
        self._path       = db_cfg["path"]
        self._wal        = db_cfg.get("wal_mode", True)
        self._cache_kb   = db_cfg.get("cache_kb", 4096)
        self._flush_ivl  = db_cfg["write_queue"]["flush_interval_s"]
        self._batch_size = db_cfg["write_queue"]["flush_batch_size"]
        self._queue      = write_queue
        self._stop_flag  = stop_event
        self._conn: sqlite3.Connection | None = None

    def run(self) -> None:
        self._conn = self._connect()
        log.info("DatabaseWriter started")
        last = time.monotonic()

        while not self._stop_flag.is_set():
            due  = (time.monotonic() - last) >= self._flush_ivl
            full = len(self._queue) >= self._batch_size
            if due or full:
                self._flush()
                last = time.monotonic()
            time.sleep(0.5)

        self._flush()
        self._conn.close()
        log.info("DatabaseWriter stopped")

    def _flush(self) -> None:
        if not self._queue:
            return
        batch = []
        while self._queue:
            batch.append(self._queue.popleft())
        try:
            with self._conn:
                rows = [(e.fruit_color, e.confidence, e.action,
                         e.station, int(e.is_reject), e.sorted_at_ms)
                        for e in batch]
                self._conn.executemany(_SQL_INSERT, rows)

                from collections import Counter
                today  = datetime.now().strftime("%Y-%m-%d")
                counts = Counter(e.fruit_color for e in batch if not e.is_reject)
                rejects = sum(1 for e in batch if e.is_reject)
                self._conn.execute(_SQL_UPSERT, (
                    today,
                    counts.get("GREEN",  0),
                    counts.get("RED",    0),
                    counts.get("YELLOW", 0),
                    rejects,
                    len(batch),
                ))
            log.debug(f"DB flush: {len(batch)} events")
        except sqlite3.Error as e:
            log.error(f"DB error: {e} — re-queuing")
            for item in batch:
                self._queue.appendleft(item)

    def _connect(self) -> sqlite3.Connection:
        p = Path(self._path)
        p.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(p), check_same_thread=False)
        if self._wal:
            conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(f"PRAGMA cache_size=-{self._cache_kb}")
        conn.execute(_SQL_CREATE_EVENTS)
        conn.execute(_SQL_CREATE_STATS)
        conn.commit()
        log.info(f"Database ready: {p}")
        return conn