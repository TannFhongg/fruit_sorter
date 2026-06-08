from __future__ import annotations

import sqlite3
import threading
import time
from collections import deque

import web.flask_app as flask_app


def _cfg(db_path) -> dict:
    return {
        "web": {
            "secret_key": "test",
            "socketio_async_mode": "threading",
            "cors_allowed_origins": "*",
        },
        "database": {"path": str(db_path)},
        "dashboard": {
            "push_interval_s": 0.05,
            "camera_offline_timeout_s": 0.1,
        },
    }


class _DetectorOk:
    def health_status(self) -> dict:
        return {
            "model": {
                "status": "ok",
                "loaded": True,
                "simulation": False,
                "error": None,
            }
        }


class _SerialOk:
    @property
    def is_connected(self) -> bool:
        return True


def test_health_reports_stale_camera_frame(tmp_path):
    db_path = tmp_path / "sorter.db"
    sqlite3.connect(db_path).close()

    stop = threading.Event()
    flask_app._health_sources.clear()
    flask_app.set_health_sources(
        fruit_detector=_DetectorOk(),
        serial_link=_SerialOk(),
    )

    with flask_app._latest_frame_lock:
        flask_app._latest_frame = b"stale-jpeg"
        flask_app._latest_frame_ts = time.monotonic() - 1.0

    app, _ = flask_app.create_flask_app(_cfg(db_path), deque(), stop)
    try:
        response = app.test_client().get("/api/health")
    finally:
        stop.set()

    assert response.status_code == 503
    body = response.get_json()
    assert body["status"] == "error"
    assert body["components"]["camera"]["status"] == "error"
    assert body["components"]["camera"]["message"] == "last frame is stale"


def test_sort_done_callback_emits_sort_event_payload():
    emitted = []

    class FakeSocket:
        def emit(self, event_name, payload):
            emitted.append((event_name, payload))

    old_socket = flask_app._socketio_ref
    flask_app._socketio_ref = FakeSocket()
    flask_app._live.clear()
    try:
        flask_app._on_sort_done(
            fruit_color="GREEN",
            confidence=0.91,
            action="SERVO1_FIRE",
            station=1,
            is_reject=False,
            ts_ms=1_800_000_000_000,
        )
    finally:
        flask_app._socketio_ref = old_socket
        flask_app._live.clear()

    assert emitted == [
        (
            "sort_event",
            {
                "fruit_color": "GREEN",
                "confidence": 0.91,
                "action": "SERVO1_FIRE",
                "station": 1,
                "is_reject": False,
                "ts_ms": 1_800_000_000_000,
            },
        )
    ]


def test_live_stats_total_includes_rejects(tmp_path):
    stop = threading.Event()
    flask_app._live.clear()

    app, _ = flask_app.create_flask_app(_cfg(tmp_path / "sorter.db"), deque(), stop)
    try:
        flask_app._on_sort_done("GREEN", is_reject=False)
        flask_app._on_sort_done("RED", is_reject=True)

        response = app.test_client().get("/api/stats/live")
    finally:
        stop.set()
        flask_app._live.clear()

    assert response.status_code == 200
    body = response.get_json()
    assert body["GREEN"] == 1
    assert body["rejects"] == 1
    assert body["total"] == 2


def test_stats_today_returns_500_when_db_query_fails(tmp_path):
    db_path = tmp_path / "sorter.db"
    sqlite3.connect(db_path).close()

    stop = threading.Event()
    app, _ = flask_app.create_flask_app(_cfg(db_path), deque(), stop)
    try:
        response = app.test_client().get("/api/stats/today")
    finally:
        stop.set()

    assert response.status_code == 500
    assert response.get_json()["error"] == "database query failed"
