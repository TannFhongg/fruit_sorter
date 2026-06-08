"""
database/db_writer.py
Background thread: gom batch SortEvent từ write_queue → SQLite WAL.

Bug fix — Re-enqueue infinite loop
------------------------------------
PROBLEM: The previous implementation re-enqueued the entire batch on
ANY sqlite3.Error:

    except sqlite3.Error as e:
        log.error(f"DB error: {e} — re-queuing")
        for item in batch:
            self._queue.appendleft(item)   # ← BUG

When the error is *persistent* (disk full, file corrupted, wrong
permissions, filesystem unmounted), this creates an infinite loop:

  1. Dequeue batch  → INSERT fails  → re-enqueue with appendleft
  2. Next _flush()  → dequeue same batch  → INSERT fails  → re-enqueue
  3. Repeat forever: CPU spins at 100 %, log fills disk, queue never drains.

Additionally, `deque(maxlen=200)` makes `appendleft` evict the *newest*
items (from the right end) rather than old ones, so live production data
is silently dropped while the stuck batch churns indefinitely.

FIXED PATTERN — retry with exponential backoff, then drop:
  • Attempt the flush up to _MAX_FLUSH_RETRIES times (default 3).
  • Between attempts: sleep 0.5 s × attempt number (0.5 s, 1.0 s).
    This handles transient errors: brief I/O spike, WAL checkpoint
    contention, temporary lock from another process.
  • After all retries exhausted: log CRITICAL and DROP the batch.
    Data loss is explicitly acknowledged in the log message with enough
    context (path, batch size, error) to diagnose and recover manually.
  • Never re-enqueue on persistent failure — queue stays healthy and
    new events from the running sorter continue to accumulate normally.

Rationale for dropping vs. persisting to a fallback:
  A fallback file could itself be on the same full/broken filesystem.
  Dropping with a CRITICAL log is the safest, simplest behaviour that
  keeps the rest of the system running.  An operator monitoring logs
  will see the alert and can investigate.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from collections import Counter, deque
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)

# Maximum number of consecutive flush attempts before giving up and
# dropping the batch.  Each retry waits 0.5 s × attempt number.
_MAX_FLUSH_RETRIES = 3

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


def _local_date_from_ms(timestamp_ms: float) -> str:
    """Return the local calendar date for an event wall-clock timestamp."""
    return datetime.fromtimestamp(timestamp_ms / 1000).strftime("%Y-%m-%d")


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
        self._last_error: str | None = None
        self._last_flush_ok_ts: float = 0.0
        self._dropped_batches = 0

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

        # Drain the queue into a local batch.
        # The batch is exclusively owned by this thread from here on —
        # no lock needed because DatabaseWriter is the only consumer.
        batch = []
        while self._queue:
            batch.append(self._queue.popleft())

        for attempt in range(1, _MAX_FLUSH_RETRIES + 1):
            try:
                self._write_batch(batch)
                self._last_error = None
                self._last_flush_ok_ts = time.time()
                log.debug("DB flush: %d events (attempt %d)", len(batch), attempt)
                return  # ← success: exit retry loop immediately

            except sqlite3.Error as exc:
                log.error(
                    "DB flush attempt %d/%d failed (%d events): %s",
                    attempt, _MAX_FLUSH_RETRIES, len(batch), exc,
                )
                if attempt < _MAX_FLUSH_RETRIES:
                    # Exponential-ish backoff: 0.5 s, 1.0 s
                    time.sleep(0.5 * attempt)
                else:
                    # All retries exhausted — persistent error.
                    # DROP the batch rather than re-enqueue to avoid:
                    #   • infinite loop on persistent failures
                    #   • CPU spinning at 100 %
                    #   • newest live events being silently evicted by
                    #     appendleft on a bounded deque
                    log.critical(
                        "DB flush failed after %d attempts — DROPPING %d events. "
                        "Investigate: path=%s  error=%s",
                        _MAX_FLUSH_RETRIES, len(batch), self._path, exc,
                    )
                    self._last_error = str(exc)
                    self._dropped_batches += 1
                    # Intentionally do NOT re-enqueue.
                    # The queue remains healthy; new events continue
                    # to accumulate so the sorter keeps running.

    def health_status(self) -> dict:
        if self._last_error:
            status = "error"
        elif self._conn is None:
            status = "starting"
        else:
            status = "ok"

        return {
            "status": status,
            "path": self._path,
            "queue_depth": len(self._queue),
            "last_error": self._last_error,
            "last_flush_ok_ts": self._last_flush_ok_ts,
            "dropped_batches": self._dropped_batches,
        }

    def _write_batch(self, batch: list) -> None:
        """Execute one atomic SQLite transaction for the given batch.
        Raises sqlite3.Error on any failure — caller handles retries."""
        
        rows = [
            (e.fruit_color, e.confidence, e.action,
             e.station, int(e.is_reject), e.sorted_at_ms)
            for e in batch
        ]
        stats_by_date: dict[str, Counter] = {}
        for event in batch:
            counts = stats_by_date.setdefault(
                _local_date_from_ms(event.sorted_at_ms),
                Counter(),
            )
            if event.is_reject:
                counts["rejects"] += 1
            else:
                counts[event.fruit_color] += 1
            counts["total"] += 1

        with self._conn:
            self._conn.executemany(_SQL_INSERT, rows)
            self._conn.executemany(
                _SQL_UPSERT,
                [
                    (
                        day,
                        counts.get("GREEN",  0),
                        counts.get("RED",    0),
                        counts.get("YELLOW", 0),
                        counts.get("rejects", 0),
                        counts.get("total",   0),
                    )
                    for day, counts in stats_by_date.items()
                ],
            )

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
        log.info("Database ready: %s", p)
        return conn
