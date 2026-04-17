"""
main.py — Entry Point · FruitSorter
=====================================
Flask web server + khởi động toàn bộ background threads:
  · Thread 1  : perception/fruit_detector.py  (Camera → YOLO → Queue)
  · Thread 2  : control/sort_controller.py    (Serial bridge → Arduino Slave)
  · Thread 3  : Flask dev server / SocketIO   (Dashboard + REST API)
  · Background: database/db_writer.py         (Batch SQLite writer)

Chạy:
    python main.py
    python main.py --config config/hardware_config.yaml --debug
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
from collections import deque

from config.loader import load_config
from perception.fruit_detector import FruitDetector
from control.sort_controller import SortController
from drivers.serial_link import SerialLink
from database.db_writer import DatabaseWriter
from shared.detection_result import DetectionResult


# ── CLI args ───────────────────────────────────────────────────────────────
parser = argparse.ArgumentParser(description="FruitSorter — RPi Master")
parser.add_argument("--config", default="config/hardware_config.yaml")
parser.add_argument("--debug",  action="store_true")
args = parser.parse_args()

# ── Config & logging ───────────────────────────────────────────────────────
cfg = load_config(args.config)
logging.basicConfig(
    level=logging.DEBUG if args.debug else logging.INFO,
    format="%(asctime)s [%(threadName)-16s] %(levelname)s — %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(cfg["system"]["log_file"], encoding="utf-8"),
    ],
)
log = logging.getLogger("main")

# ── Shared state ───────────────────────────────────────────────────────────
stop_event       = threading.Event()
detection_queue  = deque(maxlen=cfg["system"]["queue_maxlen"])
queue_lock       = threading.Lock()
db_write_queue   = deque(maxlen=200)

# ── Flask app (imported here to keep circular-import-free) ────────────────
from web.flask_app import create_flask_app   # noqa: E402  (after cfg is ready)
flask_app, socketio = create_flask_app(cfg, db_write_queue, stop_event)


def main() -> None:
    log.info("=" * 55)
    log.info("  FruitSorter Master — starting")
    log.info("=" * 55)

    # ── Serial link to Arduino Slave ──────────────────────────────
    serial_link = SerialLink(cfg, stop_event)

    # ── Thread 1: Perception (Camera + YOLO) ──────────────────────
    fruit_detector = FruitDetector(
        cfg=cfg,
        detection_queue=detection_queue,
        queue_lock=queue_lock,
        stop_event=stop_event,
        name="T1-Perception",
        daemon=True,
    )

    # ── Thread 2: Control (Serial bridge → Arduino) ───────────────
    sort_controller = SortController(
        cfg=cfg,
        serial_link=serial_link,
        detection_queue=detection_queue,
        queue_lock=queue_lock,
        db_write_queue=db_write_queue,
        stop_event=stop_event,
        name="T2-Control",
        daemon=True,
    )

    # ── Background DB writer ───────────────────────────────────────
    db_writer = DatabaseWriter(
        cfg=cfg,
        write_queue=db_write_queue,
        stop_event=stop_event,
        name="DB-Writer",
        daemon=True,
    )

    # ── Graceful shutdown ──────────────────────────────────────────
    def _shutdown(signum, frame):
        log.info("Shutdown signal — stopping all threads...")
        stop_event.set()
        sys.exit(0)

    signal.signal(signal.SIGINT,  _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    # ── Start background threads ───────────────────────────────────
    for t in (serial_link, fruit_detector, sort_controller, db_writer):
        t.start()
        log.info(f"Started: {t.name}")

    # ── Thread 3: Flask + SocketIO (blocking — runs on main thread) ─
    host = cfg["web"]["host"]
    port = cfg["web"]["port"]
    log.info(f"Dashboard → http://{host}:{port}")
    socketio.run(
        flask_app,
        host=host,
        port=port,
        debug=args.debug,
        use_reloader=False,   # MUST be False — we manage threads manually
        allow_unsafe_werkzeug=True,
    )


if __name__ == "__main__":
    main()