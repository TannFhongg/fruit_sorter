"""
web/flask_app.py  ·  Thread 3 — Flask + Flask-SocketIO
=========================================================
Routes:
  GET  /                    → templates/index.html (Dashboard)
  GET  /video_feed          → MJPEG stream từ shared frame buffer
  GET  /api/stats/live
  GET  /api/stats/today
  GET  /api/stats/history   ?days=7
  GET  /api/events/recent   ?limit=50
  GET  /api/health
  WS   stats_update         → broadcast mỗi push_interval_s
  WS   detection            → push ngay khi FruitDetector detect được vật thể
  WS   sort_event           → push ngay khi servo kích
"""

from __future__ import annotations

import logging
import threading
import time
from collections import defaultdict, deque

from flask import Flask, Response, jsonify, render_template, request
from flask_socketio import SocketIO

import database.db_queries as Q

log = logging.getLogger(__name__)

# ── In-memory live counters ────────────────────────────────────────────────
_live: dict = defaultdict(int)

# ── Shared latest frame (numpy array, set bởi FruitDetector) ─────────────
_latest_frame_lock = threading.Lock()
_latest_frame      = None   # bytes (JPEG-encoded) hoặc None


def update_live_count(color: str, is_reject: bool = False) -> None:
    """Gọi từ SortController (thread 2) sau mỗi sort event."""
    if is_reject:
        _live["rejects"] += 1
    else:
        _live[color] += 1


def push_frame(jpeg_bytes: bytes) -> None:
    """
    Gọi từ FruitDetector (thread 1) mỗi frame.
    Lưu frame mới nhất vào buffer để /video_feed stream ra.
    """
    global _latest_frame
    with _latest_frame_lock:
        _latest_frame = jpeg_bytes


def push_detection_event(label: str, confidence: float) -> None:
    """
    Gọi từ FruitDetector khi detect được vật thể (trước khi vào queue).
    Push ngay lên dashboard qua SocketIO.
    """
    if _socketio_ref:
        _socketio_ref.emit("detection", {
            "label":      label,
            "confidence": confidence,
            "ts":         time.time(),
        })


def push_sort_event(event_data: dict) -> None:
    """Gọi từ SortController sau khi servo kích xong."""
    if _socketio_ref:
        _socketio_ref.emit("sort_event", event_data)


# Ref đến socketio để push từ thread khác
_socketio_ref: SocketIO | None = None


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

    db_path  = cfg["database"]["path"]
    push_ivl = cfg["dashboard"]["push_interval_s"]

    # ── HTML Dashboard ─────────────────────────────────────────────────────

    @app.route("/")
    def dashboard():
        return render_template("index.html")

    # ── MJPEG Video stream ─────────────────────────────────────────────────

    @app.route("/video_feed")
    def video_feed():
        """
        MJPEG stream endpoint.
        Browser gọi <img src="/video_feed"> và nhận stream liên tục.
        Frame được set bởi FruitDetector qua push_frame().
        """
        def generate():
            import cv2
            import numpy as np

            # Placeholder frame khi chưa có camera data
            _placeholder = None

            while not stop_event.is_set():
                with _latest_frame_lock:
                    frame_bytes = _latest_frame

                if frame_bytes is None:
                    # Tạo placeholder frame màu đen với text
                    if _placeholder is None:
                        ph = np.zeros((480, 640, 3), dtype=np.uint8)
                        cv2.putText(
                            ph, "Waiting for camera...",
                            (160, 240), cv2.FONT_HERSHEY_SIMPLEX,
                            0.8, (80, 80, 80), 1
                        )
                        _, buf = cv2.imencode(".jpg", ph, [cv2.IMWRITE_JPEG_QUALITY, 70])
                        _placeholder = buf.tobytes()
                    frame_bytes = _placeholder

                yield (
                    b"--frame\r\n"
                    b"Content-Type: image/jpeg\r\n\r\n"
                    + frame_bytes
                    + b"\r\n"
                )
                # ~30fps cap cho stream
                time.sleep(0.033)

        return Response(
            generate(),
            mimetype="multipart/x-mixed-replace; boundary=frame",
            headers={
                "Cache-Control": "no-cache, no-store, must-revalidate",
                "Pragma":        "no-cache",
                "Expires":       "0",
            }
        )

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

    # ── SocketIO background push ───────────────────────────────────────────

    def _push_loop():
        while not stop_event.is_set():
            g, r, y = _live["GREEN"], _live["RED"], _live["YELLOW"]
            socketio.emit("stats_update", {
                "GREEN":   g, "RED": r, "YELLOW": y,
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
