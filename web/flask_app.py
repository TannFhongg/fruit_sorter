"""
web/flask_app.py  ·  Thread 3 — Flask + Flask-SocketIO
=========================================================
Routes:
  GET  /                    → templates/index.html (Dashboard)
  GET  /api/stats/live      → in-memory counters
  GET  /api/stats/today     → SQLite
  GET  /api/stats/history   → ?days=7
  GET  /api/events/recent   → ?limit=50
  GET  /api/health
  WS   SocketIO event: "stats_update"  → broadcast mỗi push_interval_s
"""

from __future__ import annotations

import logging
import threading
import time
from collections import defaultdict, deque

from flask import Flask, jsonify, render_template, request
from flask_socketio import SocketIO

import database.db_queries as Q

log = logging.getLogger(__name__)

# ── In-memory live counters ────────────────────────────────────────────────
_live: dict = defaultdict(int)


def update_live_count(color: str, is_reject: bool = False) -> None:
    """Gọi từ SortController (thread 2) sau mỗi sort event."""
    if is_reject:
        _live["rejects"] += 1
    else:
        _live[color] += 1


def create_flask_app(
    cfg: dict,
    db_write_queue: deque,
    stop_event: threading.Event,
) -> tuple[Flask, SocketIO]:

    app = Flask(
        __name__,
        template_folder="../templates",
        static_folder="../static",
    )
    app.config["SECRET_KEY"] = cfg["web"]["secret_key"]

    socketio = SocketIO(
        app,
        async_mode=cfg["web"]["socketio_async_mode"],
        cors_allowed_origins=cfg["web"]["cors_allowed_origins"],
        logger=False,
        engineio_logger=False,
    )

    db_path   = cfg["database"]["path"]
    push_ivl  = cfg["dashboard"]["push_interval_s"]

    # ── HTML Dashboard ─────────────────────────────────────────────────────

    @app.route("/")
    def dashboard():
        return render_template("index.html")

    # ── REST API ───────────────────────────────────────────────────────────

    @app.route("/api/health")
    def health():
        return jsonify({"status": "ok", "ts": time.time()})

    @app.route("/api/stats/live")
    def stats_live():
        g, r, y = _live["GREEN"], _live["RED"], _live["YELLOW"]
        return jsonify({
            "GREEN": g, "RED": r, "YELLOW": y,
            "rejects": _live["rejects"],
            "total": g + r + y,
            "ts": time.time(),
        })

    @app.route("/api/stats/today")
    def stats_today():
        return jsonify(Q.get_today_stats(db_path))

    @app.route("/api/stats/history")
    def stats_history():
        days = request.args.get("days", 7, type=int)
        return jsonify(Q.get_history(db_path, days))

    @app.route("/api/events/recent")
    def events_recent():
        limit = request.args.get("limit", 50, type=int)
        return jsonify(Q.get_recent_events(db_path, limit))

    @app.route("/api/stats/hourly")
    def stats_hourly():
        return jsonify(Q.get_hourly_breakdown(db_path))

    # ── SocketIO background push ───────────────────────────────────────────

    def _push_loop():
        while not stop_event.is_set():
            g, r, y = _live["GREEN"], _live["RED"], _live["YELLOW"]
            socketio.emit("stats_update", {
                "GREEN":   g,
                "RED":     r,
                "YELLOW":  y,
                "rejects": _live["rejects"],
                "total":   g + r + y,
                "ts":      time.time(),
            })
            time.sleep(push_ivl)

    socketio.start_background_task(_push_loop)

    @socketio.on("connect")
    def on_connect():
        log.info(f"SocketIO client connected: {request.sid}")

    @socketio.on("disconnect")
    def on_disconnect():
        log.info(f"SocketIO client disconnected: {request.sid}")

    return app, socketio