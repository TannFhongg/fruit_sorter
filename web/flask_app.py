"""
web/flask_app.py  ·  Thread 3 — Flask + Flask-SocketIO
=========================================================
Routes:
  GET  /                     → templates/index.html (Dashboard)
  GET  /video_feed           → MJPEG stream from shared frame buffer
  GET  /api/stats/live
  GET  /api/stats/today
  GET  /api/stats/history    ?days=7
  GET  /api/events/recent    ?limit=50
  GET  /api/health
  WS   stats_update          → broadcast every push_interval_s
  WS   detection             → pushed when FruitDetector detects an object
  WS   sort_event            → pushed when a servo fires

Circular-import fix (Issue #3)
-------------------------------
Previously, fruit_detector.py and sort_controller.py imported
push_frame / push_detection_event / update_live_count directly from this
module.  This created a fragile load-order dependency: flask_app had to
be fully initialised before those modules imported it, but flask_app
itself is set up *after* the other modules are constructed in main.py.

Fix: this module no longer exports any push_* functions for other modules
to call.  Instead, it *subscribes* to events on the shared event bus at
startup.  Perception and control modules publish events; this module
reacts to them.  The dependency arrow is reversed — only this module
knows about the event bus; perception and control do not know this module
exists.

Wiring is done in create_flask_app(), which is called once by main.py
after all modules are constructed.

Live Counter Reset
------------------
_live counters track in-memory stats for the current day. They are reset
at midnight (00:00 local time) to ensure /api/stats/live matches
/api/stats/today from the database.

The reset is checked in _push_loop() every push_interval_s (typically 1s).
When a new day is detected (date changes), counters are zeroed.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from collections import defaultdict, deque
from datetime import datetime
from pathlib import Path

from flask import Flask, Response, jsonify, render_template, request
from flask_socketio import SocketIO

import database.db_queries as Q
from shared.event_bus import EVT_DETECTION, EVT_FRAME, EVT_SORT_DONE, bus

log = logging.getLogger(__name__)

# ── In-memory live counters (written by _on_sort_done, read by push loop) ──
_live: dict = defaultdict(int)
_live_lock  = threading.Lock()   # guards _live across threads
_live_date: str = ""              # tracks current date for midnight reset

# ── Shared latest frame (JPEG bytes) ──────────────────────────────────────
_latest_frame_lock = threading.Lock()
_latest_frame: bytes | None = None
_latest_frame_ts: float = 0.0
_app_started_ts: float = time.monotonic()

# ── SocketIO singleton ref (set inside create_flask_app) ─────────────────
_socketio_ref: SocketIO | None = None

# ── Runtime health sources, registered by main.py after construction ──────
_health_sources: dict[str, object] = {}


def set_health_sources(**sources: object) -> None:
    """Register runtime objects used by /api/health."""
    _health_sources.update({k: v for k, v in sources.items() if v is not None})


# ── Event-bus callbacks ───────────────────────────────────────────────────
#
# These are registered in create_flask_app() and called by the emitter's
# thread (CaptureThread / InferenceLoop / SortController).
# They must be fast and non-blocking.

def _on_frame(jpeg_bytes: bytes) -> None:
    """Store the latest camera frame for the MJPEG endpoint."""
    global _latest_frame, _latest_frame_ts
    with _latest_frame_lock:
        _latest_frame = bytes(jpeg_bytes)
        _latest_frame_ts = time.monotonic()


def _on_detection(label: str, confidence: float) -> None:
    """Push a detection event to all connected dashboard clients."""
    if _socketio_ref:
        _socketio_ref.emit("detection", {
            "label":      label,
            "confidence": confidence,
            "ts":         time.time(),
        })


def _on_sort_done(
    fruit_color: str,
    is_reject: bool,
    confidence: float | None = None,
    action: str | None = None,
    station: int | None = None,
    ts_ms: float | None = None,
) -> None:
    """Update counters and push a completed sort event to dashboards."""
    with _live_lock:
        if is_reject:
            _live["rejects"] += 1
        else:
            _live[fruit_color] += 1

    if _socketio_ref:
        _socketio_ref.emit("sort_event", {
            "fruit_color": fruit_color,
            "confidence": 0.0 if confidence is None else confidence,
            "action": action or ("REJECT" if is_reject else "PASS"),
            "station": station or 1,
            "is_reject": is_reject,
            "ts_ms": ts_ms or (time.time() * 1000),
        })


def _detector_health_snapshot() -> dict:
    detector = _health_sources.get("fruit_detector")
    if detector is None or not hasattr(detector, "health_status"):
        return {"status": "unknown", "message": "fruit detector is not registered"}

    try:
        return detector.health_status()
    except Exception as exc:
        log.exception("Detector health check failed")
        return {"status": "error", "message": str(exc)}


def _camera_health(timeout_s: float, detector_health: dict) -> dict:
    now = time.monotonic()
    with _latest_frame_lock:
        has_frame = _latest_frame is not None
        frame_ts = _latest_frame_ts

    device = detector_health.get("camera_device")
    if isinstance(device, dict) and device.get("status") == "error":
        return {
            "status": "error",
            "has_frame": has_frame,
            "age_s": None,
            "timeout_s": timeout_s,
            "device": device,
            "message": device.get("error") or "camera device error",
        }

    if has_frame and frame_ts > 0:
        age_s = now - frame_ts
        status = "ok" if age_s <= timeout_s else "error"
        message = None if status == "ok" else "last frame is stale"
    else:
        age_s = None
        status = "starting" if (now - _app_started_ts) <= timeout_s else "error"
        message = "waiting for first frame" if status == "starting" else "no frame received"

    return {
        "status": status,
        "has_frame": has_frame,
        "age_s": age_s,
        "timeout_s": timeout_s,
        "device": device,
        "message": message,
    }


def _model_health(detector_health: dict) -> dict:
    return detector_health.get("model", {"status": "unknown"})


def _serial_health() -> dict:
    serial_link = _health_sources.get("serial_link")
    if serial_link is None:
        return {"status": "unknown", "message": "serial link is not registered"}

    try:
        connected = bool(serial_link.is_connected)
    except Exception as exc:
        log.exception("Serial health check failed")
        return {"status": "error", "message": str(exc)}

    return {
        "status": "ok" if connected else "error",
        "connected": connected,
    }


def _database_health(db_path: str, db_write_queue: deque) -> dict:
    path = Path(db_path)
    writer = _health_sources.get("db_writer")
    writer_status = None
    if writer is not None and hasattr(writer, "health_status"):
        try:
            writer_status = writer.health_status()
        except Exception as exc:
            log.exception("Database writer health check failed")
            writer_status = {"status": "error", "message": str(exc)}

    if not path.exists():
        return {
            "status": "error",
            "path": str(path),
            "queue_depth": len(db_write_queue),
            "writer": writer_status,
            "message": "database file does not exist",
        }

    try:
        uri = f"file:{path.resolve()}?mode=ro"
        with sqlite3.connect(uri, uri=True, timeout=0.2) as conn:
            conn.execute("SELECT 1").fetchone()
    except sqlite3.Error as exc:
        return {
            "status": "error",
            "path": str(path),
            "queue_depth": len(db_write_queue),
            "writer": writer_status,
            "message": str(exc),
        }

    if writer_status and writer_status.get("status") == "error":
        return {
            "status": "error",
            "path": str(path),
            "queue_depth": len(db_write_queue),
            "writer": writer_status,
            "message": writer_status.get("last_error") or writer_status.get("message"),
        }

    return {
        "status": writer_status.get("status", "ok") if writer_status else "ok",
        "path": str(path),
        "queue_depth": len(db_write_queue),
        "writer": writer_status,
    }


def _aggregate_status(components: dict[str, dict], stop_requested: bool) -> str:
    if stop_requested:
        return "error"

    statuses = {c.get("status", "unknown") for c in components.values()}
    if "error" in statuses:
        return "error"
    if statuses - {"ok"}:
        return "degraded"
    return "ok"


# ── Factory ───────────────────────────────────────────────────────────────

def create_flask_app(
    cfg: dict,
    db_write_queue: deque,
    stop_event: threading.Event,
) -> tuple[Flask, SocketIO]:
    global _socketio_ref

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
    _socketio_ref = socketio

    @app.errorhandler(sqlite3.Error)
    def database_api_error(exc: sqlite3.Error):
        return jsonify({
            "error": "database query failed",
            "message": str(exc),
        }), 500

    # ── Wire event bus → this module ──────────────────────────────────────
    #
    # All subscriptions happen here, after socketio is assigned to
    # _socketio_ref, so callbacks that emit SocketIO events are safe.
    bus.subscribe(EVT_FRAME,     _on_frame)
    bus.subscribe(EVT_DETECTION, _on_detection)
    bus.subscribe(EVT_SORT_DONE, _on_sort_done)
    log.info("flask_app: subscribed to event bus (frame / detection / sort_done)")

    db_path  = cfg["database"]["path"]
    push_ivl = cfg["dashboard"]["push_interval_s"]
    camera_offline_timeout_s = float(
        cfg.get("dashboard", {}).get("camera_offline_timeout_s", 5.0)
    )

    # ── HTML Dashboard ─────────────────────────────────────────────────────

    @app.route("/")
    def dashboard() -> str:
        return render_template("index.html")

    # ── MJPEG Video stream ─────────────────────────────────────────────────

    @app.route("/video_feed")
    def video_feed() -> Response:
        """
        MJPEG stream endpoint.
        The browser renders <img src="/video_feed"> as a live video feed.

        Frame lifecycle:
          • FruitDetector running  : _on_frame() updates _latest_frame continuously
          • FruitDetector stopped  : last frame is held; overlay shows "offline"
          • No frame ever received : black placeholder shown

        The generator loop exits only on GeneratorExit (browser disconnect),
        never on stop_event, so the stream is resilient to transient errors
        in other threads.
        """
        import cv2
        import numpy as np

        def _make_placeholder(text: str = "Waiting for camera...") -> bytes:
            ph = np.zeros((480, 640, 3), dtype=np.uint8)
            cv2.putText(
                ph, text,
                (140, 240), cv2.FONT_HERSHEY_SIMPLEX,
                0.8, (80, 80, 80), 1,
            )
            _, buf = cv2.imencode(".jpg", ph, [cv2.IMWRITE_JPEG_QUALITY, 70])
            return buf.tobytes()

        placeholder_waiting = _make_placeholder("Waiting for camera...")
        placeholder_offline = _make_placeholder("Camera offline")

        def generate():
            while True:
                try:
                    # ── CRITICAL: Copy frame bytes inside lock ────────────
                    # _latest_frame is reassigned at 30fps by _on_frame().
                    # We must copy the bytes object while holding the lock
                    # to ensure the reference remains valid after lock release.
                    # 
                    # Although Python bytes are immutable, the _latest_frame
                    # *reference* can be reassigned. If we only capture the
                    # reference and yield outside the lock, the old bytes
                    # object could be GC'd before yield completes (especially
                    # under threading mode with SocketIO).
                    #
                    # Copying ensures we own a stable reference for the
                    # entire yield cycle.
                    
                    with _latest_frame_lock:
                        if _latest_frame is not None:
                            frame_bytes = bytes(_latest_frame)
                            frame_ts = _latest_frame_ts
                        else:
                            frame_bytes = None
                            frame_ts = 0.0

                    now = time.monotonic()

                    if (
                        frame_bytes is not None
                        and frame_ts > 0.0
                        and (now - frame_ts) <= camera_offline_timeout_s
                    ):
                        out_bytes = frame_bytes
                    elif frame_ts == 0.0:
                        out_bytes = placeholder_waiting
                    else:
                        out_bytes = placeholder_offline

                    yield (
                        b"--frame\r\n"
                        b"Content-Type: image/jpeg\r\n\r\n"
                        + out_bytes
                        + b"\r\n"
                    )
                    time.sleep(0.033)  # ~30 fps cap

                except GeneratorExit:
                    break

        return Response(
            generate(),
            mimetype="multipart/x-mixed-replace; boundary=frame",
            headers={
                "Cache-Control": "no-cache, no-store, must-revalidate",
                "Pragma":        "no-cache",
                "Expires":       "0",
            },
        )

    # ── REST API ───────────────────────────────────────────────────────────

    @app.route("/api/health")
    def health():
        detector_health = _detector_health_snapshot()
        components = {
            "camera": _camera_health(camera_offline_timeout_s, detector_health),
            "model": _model_health(detector_health),
            "serial": _serial_health(),
            "database": _database_health(db_path, db_write_queue),
        }
        status = _aggregate_status(components, stop_event.is_set())
        return jsonify({
            "status": status,
            "components": components,
            "stop_requested": stop_event.is_set(),
            "ts": time.time(),
        }), (200 if status == "ok" else 503)

    @app.route("/api/stats/live")
    def stats_live():
        with _live_lock:
            g   = _live["GREEN"]
            r   = _live["RED"]
            y   = _live["YELLOW"]
            rej = _live["rejects"]
        return jsonify({
            "GREEN":   g,
            "RED":     r,
            "YELLOW":  y,
            "rejects": rej,
            "total":   g + r + y + rej,
            "ts":      time.time(),
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

    # ── SocketIO — background stats push ──────────────────────────────────

    def _push_loop() -> None:
        global _live_date
        
        # Initialize current date
        with _live_lock:
            _live_date = datetime.now().strftime("%Y-%m-%d")
        
        log.info("Stats push loop started, current date: %s", _live_date)
        
        while not stop_event.is_set():
            # ── Check for midnight reset ───────────────────────────────────
            # If date has changed, reset all live counters to zero.
            # This ensures /api/stats/live matches /api/stats/today.
            
            current_date = datetime.now().strftime("%Y-%m-%d")
            
            with _live_lock:
                if current_date != _live_date:
                    # New day detected - reset counters
                    old_date = _live_date
                    _live_date = current_date
                    
                    # Log old values before reset
                    old_green = _live["GREEN"]
                    old_red = _live["RED"]
                    old_yellow = _live["YELLOW"]
                    old_rejects = _live["rejects"]
                    
                    # Reset all counters
                    # _live is defaultdict(int), so after clear(), any key
                    # access automatically returns 0. No need to explicitly
                    # set each key to 0.
                    _live.clear()
                    
                    log.info(
                        "Midnight reset: %s → %s | Previous day totals: "
                        "GREEN=%d, RED=%d, YELLOW=%d, rejects=%d",
                        old_date, current_date,
                        old_green, old_red, old_yellow, old_rejects
                    )
                
                # Read current values for broadcast
                g   = _live["GREEN"]
                r   = _live["RED"]
                y   = _live["YELLOW"]
                rej = _live["rejects"]
            
            # Broadcast to all connected clients
            socketio.emit("stats_update", {
                "GREEN":   g,
                "RED":     r,
                "YELLOW":  y,
                "rejects": rej,
                "total":   g + r + y + rej,
                "ts":      time.time(),
            })
            time.sleep(push_ivl)

    socketio.start_background_task(_push_loop)

    @socketio.on("connect")
    def on_connect():
        log.info("SocketIO client connected: %s", request.sid)

    @socketio.on("disconnect")
    def on_disconnect():
        log.info("SocketIO client disconnected: %s", request.sid)

    return app, socketio
