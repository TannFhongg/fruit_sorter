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
import threading
import time
from collections import defaultdict, deque
from datetime import datetime

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

# ── SocketIO singleton ref (set inside create_flask_app) ─────────────────
_socketio_ref: SocketIO | None = None


# ── Event-bus callbacks ───────────────────────────────────────────────────
#
# These are registered in create_flask_app() and called by the emitter's
# thread (CaptureThread / InferenceLoop / SortController).
# They must be fast and non-blocking.

def _on_frame(jpeg_bytes: bytes) -> None:
    """Store the latest camera frame for the MJPEG endpoint."""
    global _latest_frame
    with _latest_frame_lock:
        _latest_frame = jpeg_bytes


def _on_detection(label: str, confidence: float) -> None:
    """Push a detection event to all connected dashboard clients."""
    if _socketio_ref:
        _socketio_ref.emit("detection", {
            "label":      label,
            "confidence": confidence,
            "ts":         time.time(),
        })


def _on_sort_done(fruit_color: str, is_reject: bool) -> None:
    """Update in-memory live counters when a sort completes."""
    with _live_lock:
        if is_reject:
            _live["rejects"] += 1
        else:
            _live[fruit_color] += 1


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

        last_real_frame_ts = 0.0
        OFFLINE_TIMEOUT    = 5.0  # seconds

        def generate():
            nonlocal last_real_frame_ts
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
                            # Copy bytes to ensure stable reference
                            frame_bytes = bytes(_latest_frame)
                        else:
                            frame_bytes = None

                    now = time.monotonic()

                    if frame_bytes is not None:
                        last_real_frame_ts = now
                        out_bytes = frame_bytes
                    elif last_real_frame_ts == 0.0:
                        out_bytes = placeholder_waiting
                    elif (now - last_real_frame_ts) > OFFLINE_TIMEOUT:
                        out_bytes = placeholder_offline
                    else:
                        out_bytes = placeholder_waiting

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
        return jsonify({"status": "ok", "ts": time.time()})

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
            "total":   g + r + y,
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
                    _live.clear()
                    _live["GREEN"] = 0
                    _live["RED"] = 0
                    _live["YELLOW"] = 0
                    _live["rejects"] = 0
                    
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
                "total":   g + r + y,
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