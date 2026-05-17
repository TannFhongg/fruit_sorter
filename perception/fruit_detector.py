"""
perception/fruit_detector.py  ·  Thread 1 — Perception (HIGH-FPS VERSION)
=========================================================================
Two-thread internal architecture:
  CaptureThread  : reads camera frames continuously into a FrameBuffer (deque maxlen=2)
  InferenceLoop  : pulls the latest frame from the buffer, runs NCNN, pushes results

Result: camera never waits for NCNN; NCNN never waits for camera.
Target: 25–30 FPS on RPi4 with 320×320 input.

=======================================================================
Bug Fix — Frame skip causes duplicate detection enqueue
=======================================================================
PROBLEM (original code):

    if self._skip_counter >= self._skip_n:
        self._skip_counter = 0
        self._last_dets = self._run_inference(frame)   # every N frames

    for det in self._last_dets:           # ← runs EVERY frame
        result = self._build_result(det)
        if result:
            with self.lock:
                self.queue.append(result)  # pushed N times for same fruit!

_last_dets persists between frames. With frame_skip=3, the same
detection is enqueued 3 times. SortController sees 3 entries for
one fruit and sends 3 SORT commands → servo fires 3 times.

FIXED PATTERN — dequeue only on inference frames:

    ran_inference = False
    if self._skip_counter >= self._skip_n:
        self._skip_counter = 0
        self._last_dets    = self._run_inference(frame)
        ran_inference      = True

    if ran_inference:               # ← only push on the frame we inferred
        for det in self._last_dets:
            ...
            self.queue.append(result)

_last_dets is still kept for drawing bounding boxes on the video
stream (the EVT_FRAME path), but it is never used for queue pushes
on non-inference frames.

Why not clear _last_dets on non-inference frames?
    Clearing would be correct for the queue, but the visual overlay
    would flicker every N frames (bounding box appears 1 frame out of
    every N). Keeping the separation between "draw" and "enqueue"
    is the right architectural fix: each concern uses _last_dets
    differently and that's fine.

=======================================================================
Circular-import fix (Issue #3) — unchanged from v2
=======================================================================
Publishes through the shared event bus instead of importing flask_app.
    bus.emit(EVT_FRAME, jpeg_bytes)
    bus.emit(EVT_DETECTION, label=..., confidence=...)
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from typing import Optional

import cv2
import numpy as np

from shared.detection_result import DetectionResult, FruitColor, SortAction
from shared.event_bus import EVT_DETECTION, EVT_FRAME, bus

log = logging.getLogger(__name__)


# ── NMS helper ────────────────────────────────────────────────────────────

def _nms(boxes: np.ndarray, scores: np.ndarray, iou_thr: float) -> list[int]:
    if len(boxes) == 0:
        return []
    x1 = boxes[:, 0] - boxes[:, 2] / 2
    y1 = boxes[:, 1] - boxes[:, 3] / 2
    x2 = boxes[:, 0] + boxes[:, 2] / 2
    y2 = boxes[:, 1] + boxes[:, 3] / 2
    areas = (x2 - x1) * (y2 - y1)
    order = scores.argsort()[::-1]
    keep: list[int] = []
    while order.size > 0:
        i = order[0]
        keep.append(int(i))
        if order.size == 1:
            break
        inter = (
            np.maximum(0, np.minimum(x2[i], x2[order[1:]]) - np.maximum(x1[i], x1[order[1:]])) *
            np.maximum(0, np.minimum(y2[i], y2[order[1:]]) - np.maximum(y1[i], y1[order[1:]]))
        )
        iou   = inter / (areas[i] + areas[order[1:]] - inter + 1e-6)
        order = order[1:][iou <= iou_thr]
    return keep


# ── Internal capture thread ────────────────────────────────────────────────

class _CaptureThread(threading.Thread):
    """
    Background thread: reads the camera as fast as possible into a
    2-slot ring buffer. InferenceLoop always picks up the freshest frame.
    maxlen=2: slot[0] = frame being inferred, slot[1] = next frame ready.
    """

    def __init__(self, cap: cv2.VideoCapture, stop_event: threading.Event):
        super().__init__(name="CaptureThread", daemon=True)
        self._cap   = cap
        self._stop  = stop_event
        self.buffer: deque = deque(maxlen=2)
        self._lock  = threading.Lock()
        self.frame_count = 0
        self.drop_count  = 0

    def get_latest(self) -> Optional[np.ndarray]:
        with self._lock:
            return self.buffer[-1] if self.buffer else None

    def run(self) -> None:
        log.info("CaptureThread started")
        while not self._stop.is_set():
            ret, frame = self._cap.read()
            if not ret:
                time.sleep(0.005)
                continue
            with self._lock:
                if len(self.buffer) == self.buffer.maxlen:
                    self.drop_count += 1
                self.buffer.append(frame)
            self.frame_count += 1
        log.info(
            "CaptureThread stopped | captured=%d dropped=%d",
            self.frame_count, self.drop_count,
        )


# ── Main detector thread ───────────────────────────────────────────────────

class FruitDetector(threading.Thread):

    def __init__(
        self,
        cfg: dict,
        detection_queue: deque,
        queue_lock: threading.Lock,
        stop_event: threading.Event,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.cfg        = cfg
        self.queue      = detection_queue
        self.lock       = queue_lock
        self.stop_event = stop_event
        self._frame_id  = 0
        self._interp    = None
        self._input_wh: tuple[int, int] = (320, 320)
        self._transposed = True

        m = cfg["model"]
        self._conf_thr = m["thresholds"]["confidence"]
        self._iou_thr  = m["thresholds"]["iou_nms"]
        self._min_area = m["thresholds"]["min_bbox_area"]
        self._labels   = m["labels"]
        self._routing  = m["routing"]

        cam = cfg["camera"]
        self._cam_idx = cam["device_index"]
        self._cam_w   = cam["width"]
        self._cam_h   = cam["height"]
        self._cam_fps = cam["fps"]
        self._cam_buf = cam["buffer_size"]

        self._skip_n       = cfg.get("model", {}).get("frame_skip", 2)
        self._skip_counter = 0

        # _last_dets: cached for OVERLAY DRAWING only.
        # NEVER used to push into the detection queue on non-inference frames.
        # See module docstring for the full explanation.
        self._last_dets: list[dict] = []

    # ── Thread body ────────────────────────────────────────────────────────

    def run(self) -> None:
        self._load_model()
        cap     = self._open_camera()
        capture = _CaptureThread(cap, self.stop_event)
        capture.start()

        # Wait for the buffer to fill before entering inference loop
        for _ in range(10):
            if capture.buffer:
                break
            time.sleep(0.01)

        cycle_times: deque = deque(maxlen=60)
        log.info("FruitDetector (T1) started — dual-thread mode")

        while not self.stop_event.is_set():
            t0 = time.monotonic()

            frame = capture.get_latest()
            if frame is None:
                time.sleep(0.001)
                continue

            self._frame_id     += 1
            self._skip_counter += 1

            # Publish JPEG frame to the event bus → flask_app streams it
            _, jpeg = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 70])
            bus.emit(EVT_FRAME, jpeg.tobytes())

            # ── Frame skip: run NCNN inference only every _skip_n frames ──
            #
            # KEY INVARIANT: detection results are pushed into the shared
            # queue ONLY on the frame where inference actually ran.
            # _last_dets is updated here and may be used for overlay drawing
            # on subsequent frames, but NEVER for queue pushes.
            #
            # Violation of this invariant causes duplicate enqueues:
            #   frame_skip=3, fruit detected → 3 identical DetectionResults
            #   → SortController sends 3 SORT commands → servo fires 3 times.

            ran_inference = False
            if self._skip_counter >= self._skip_n:
                self._skip_counter = 0
                self._last_dets    = self._run_inference(frame)
                ran_inference      = True

            if ran_inference:
                # Enqueue detections ONLY on inference frames
                for det in self._last_dets:
                    result = self._build_result(det)
                    if result:
                        with self.lock:
                            self.queue.append(result)
                        # Publish detection event → flask_app pushes to dashboard
                        bus.emit(EVT_DETECTION, label=det["label"], confidence=det["confidence"])

            elapsed = time.monotonic() - t0
            cycle_times.append(elapsed)

            if self._frame_id % 300 == 0:
                avg_ms = (sum(cycle_times) / len(cycle_times)) * 1000
                log.info(
                    "Perception: fps=%.1f | cycle=%.1fms | queue=%d | cam_drop=%d",
                    1000 / avg_ms, avg_ms, len(self.queue), capture.drop_count,
                )

        cap.release()
        log.info("FruitDetector stopped")

    # ── Model loading ──────────────────────────────────────────────────────

    def _load_model(self) -> None:
        import ncnn
        model_dir  = self.cfg["model"]["path"]
        n_threads  = self.cfg["model"].get("num_threads", 4)
        param_path = f"{model_dir}/model.ncnn.param"
        bin_path   = f"{model_dir}/model.ncnn.bin"
        log.info("Loading NCNN model: %s", model_dir)
        try:
            self._interp = ncnn.Net()
            self._interp.opt.use_vulkan_compute = False
            self._interp.opt.num_threads        = n_threads
            self._interp.load_param(param_path)
            self._interp.load_model(bin_path)
            w, h = self.cfg["model"]["input_size"]
            self._input_wh = (int(w), int(h))
            log.info("NCNN ready | input=%s | threads=%d", self._input_wh, n_threads)
        except Exception as e:
            log.error("Model load failed: %s → simulation mode", e)
            self._interp = None

    # ── Camera open ────────────────────────────────────────────────────────

    def _open_camera(self) -> cv2.VideoCapture:
        cap = cv2.VideoCapture(self._cam_idx, cv2.CAP_V4L2)
        cap.set(cv2.CAP_PROP_FRAME_WIDTH,  self._cam_w)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self._cam_h)
        cap.set(cv2.CAP_PROP_FPS,          self._cam_fps)
        cap.set(cv2.CAP_PROP_BUFFERSIZE,   self._cam_buf)
        cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
        if not cap.isOpened():
            raise RuntimeError(f"Cannot open camera {self._cam_idx}")
        log.info(
            "Camera: %d×%d @ %.0ffps",
            self._cam_w, self._cam_h, cap.get(cv2.CAP_PROP_FPS),
        )
        return cap

    # ── Inference ──────────────────────────────────────────────────────────

    def _run_inference(self, frame: np.ndarray) -> list[dict]:
        if self._interp is None:
            return self._simulate()

        import ncnn
        W, H = self._input_wh
        blob_img, (px, py, sc) = _letterbox(frame, W, H)

        mat_in = ncnn.Mat.from_pixels(
            blob_img, ncnn.Mat.PixelType.PIXEL_BGR, W, H
        )
        mat_in.substract_mean_normalize([0.0, 0.0, 0.0], [1 / 255.0] * 3)

        ex = self._interp.create_extractor()
        ex.input("in0", mat_in)
        ret, mat_out = ex.extract("out0")

        if ret != 0:
            log.warning("NCNN extract failed")
            return []

        raw = np.array(mat_out)
        if raw.ndim == 2:
            raw = raw[np.newaxis, :]
        return self._decode(raw, px, py, sc, frame.shape)

    def _decode(
        self,
        raw: np.ndarray,
        px: float, py: float, sc: float,
        orig_shape: tuple,
    ) -> list[dict]:
        out   = raw[0]
        if self._transposed:
            out = out.T
        n_cls = len(self._labels)
        bxywh = out[:, :4]
        cls_s = out[:, 4:4 + n_cls]
        cids  = np.argmax(cls_s, axis=1)
        confs = cls_s[np.arange(len(cls_s)), cids]

        mask = confs >= self._conf_thr
        if not np.any(mask):
            return []
        bxywh = bxywh[mask]
        confs = confs[mask]
        cids  = cids[mask]
        keep  = _nms(bxywh, confs, self._iou_thr)
        oh, ow = orig_shape[:2]
        results = []
        for i in keep:
            cx, cy, bw, bh = bxywh[i]
            x0 = int((cx - px) / sc - bw / (2 * sc))
            y0 = int((cy - py) / sc - bh / (2 * sc))
            w  = int(bw / sc)
            h  = int(bh / sc)
            x0, y0 = max(0, x0), max(0, y0)
            w = min(w, ow - x0)
            h = min(h, oh - y0)
            if w * h < self._min_area:
                continue
            results.append({
                "label":      self._labels.get(int(cids[i]), "UNKNOWN"),
                "confidence": float(confs[i]),
                "bbox":       (x0, y0, w, h),
            })
        return results

    def _build_result(self, det: dict) -> Optional[DetectionResult]:
        try:
            color = FruitColor(det["label"])
        except ValueError:
            color = FruitColor.UNKNOWN
        route  = self._routing.get(det["label"], self._routing.get("UNKNOWN", {}))
        action = _resolve_action(route)
        return DetectionResult(
            fruit_color=color,
            confidence=det["confidence"],
            frame_id=self._frame_id,
            bbox=det["bbox"],
            action=action,
        )

    def _simulate(self) -> list[dict]:
        import random
        if random.random() > 0.12:
            return []
        label = random.choice(["GREEN", "RED", "YELLOW"])
        return [{
            "label":      label,
            "confidence": round(random.uniform(0.70, 0.97), 2),
            "bbox":       (80, 55, 80, 80),
        }]


# ── Helpers ───────────────────────────────────────────────────────────────

def _letterbox(
    img: np.ndarray, tw: int, th: int
) -> tuple[np.ndarray, tuple[float, float, float]]:
    ih, iw  = img.shape[:2]
    sc      = min(tw / iw, th / ih)
    nw, nh  = int(iw * sc), int(ih * sc)
    resized = cv2.resize(img, (nw, nh), interpolation=cv2.INTER_LINEAR)
    canvas  = np.full((th, tw, 3), 114, dtype=np.uint8)
    px, py  = (tw - nw) // 2, (th - nh) // 2
    canvas[py:py + nh, px:px + nw] = resized
    return canvas, (float(px), float(py), sc)


def _resolve_action(route: dict) -> SortAction:
    servo     = route.get("servo")
    direction = route.get("direction", "reject")
    if servo is None or direction == "reject":
        return SortAction.REJECT
    key = f"SERVO{servo}_{direction.upper()}"
    return SortAction[key] if key in SortAction.__members__ else SortAction.REJECT