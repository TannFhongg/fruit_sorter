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
from dataclasses import dataclass
from pathlib import Path
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


def _bbox_center(bbox: tuple[int, int, int, int]) -> tuple[float, float]:
    x, y, w, h = bbox
    return x + (w / 2), y + (h / 2)


def _bbox_iou(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> float:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    ax2, ay2 = ax + aw, ay + ah
    bx2, by2 = bx + bw, by + bh

    inter_w = max(0, min(ax2, bx2) - max(ax, bx))
    inter_h = max(0, min(ay2, by2) - max(ay, by))
    inter = inter_w * inter_h
    if inter == 0:
        return 0.0

    union = (aw * ah) + (bw * bh) - inter
    return float(inter / max(union, 1))


def _center_distance(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> float:
    ax, ay = _bbox_center(a)
    bx, by = _bbox_center(b)
    return float(((ax - bx) ** 2 + (ay - by) ** 2) ** 0.5)


@dataclass
class _ObjectTrack:
    track_id: int
    label: str
    label_streak: int
    bbox: tuple[int, int, int, int]
    last_seen_frame_id: int
    last_seen_ts_ms: float
    enqueued: bool = False


# ── Internal capture thread ────────────────────────────────────────────────

class _CaptureThread(threading.Thread):
    """
    Background thread: reads the camera as fast as possible into a
    2-slot ring buffer. InferenceLoop always picks up the freshest frame.
    maxlen=2: slot[0] = frame being inferred, slot[1] = next frame ready.
    
    CRITICAL: Each frame is timestamped at capture time (not inference time)
    to ensure accurate timing for IR sensor window calculations.
    """

    def __init__(self, cap: cv2.VideoCapture, stop_event: threading.Event):
        super().__init__(name="CaptureThread", daemon=True)
        self._cap   = cap
        self._stop  = stop_event
        # Buffer now stores tuples: (frame, capture_timestamp_ms)
        self.buffer: deque = deque(maxlen=2)
        self._lock  = threading.Lock()
        self.frame_count = 0
        self.drop_count  = 0

    def get_latest(self) -> Optional[tuple[int, np.ndarray, float]]:
        """Returns (frame_id, frame, capture_timestamp_ms) or None
        
        CRITICAL: frame_count must be read inside the same lock that guards
        buffer to ensure consistency. If frame_count is incremented outside
        the lock, there's a race window where get_latest() could read
        frame_count=N but buffer contains frame N+1.
        """
        with self._lock:
            if not self.buffer:
                return None
            frame, capture_ts = self.buffer[-1]
            # Read frame_count inside lock for consistency
            return (self.frame_count, frame, capture_ts)

    def run(self) -> None:
        log.info("CaptureThread started")
        while not self._stop.is_set():
            ret, frame = self._cap.read()
            if not ret:
                time.sleep(0.005)
                continue
            # ── CRITICAL: Timestamp captured HERE, not after inference ──
            capture_ts_ms = time.monotonic() * 1000
            
            # ── CRITICAL: Increment frame_count INSIDE lock ───────────────
            # frame_count must be incremented atomically with buffer.append()
            # to prevent race condition in get_latest().
            #
            # Race scenario if frame_count is outside lock:
            #   Thread A (run):     append frame N to buffer
            #   Thread B (get_latest): read frame_count = N-1, get frame N
            #   Thread A (run):     frame_count = N
            #   → get_latest() returns (N-1, frame_N) → ID mismatch
            #
            # Fix: increment inside lock ensures frame_count always matches
            # the number of frames that have been appended to buffer.
            
            with self._lock:
                if len(self.buffer) == self.buffer.maxlen:
                    self.drop_count += 1
                self.buffer.append((frame, capture_ts_ms))
                self.frame_count += 1  # ← Moved inside lock
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
        self._system_mode = cfg.get("system", {}).get("mode", "production").lower()
        self._simulation_enabled = self._system_mode == "simulation"
        self._model_loaded = False
        self._model_error: str | None = None
        self._camera_opened = False
        self._camera_error: str | None = None

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

        tracking = cfg.get("model", {}).get("tracking", {})
        self._track_ttl_ms = float(tracking.get("ttl_ms", 600.0))
        self._track_iou_thr = float(tracking.get("iou_threshold", 0.35))
        self._track_center_dist_px = float(
            tracking.get("center_distance_px", max(self._cam_w, self._cam_h) * 0.12)
        )
        self._tracks: dict[int, _ObjectTrack] = {}
        self._next_track_id = 1

        conveyor = cfg.get("conveyor", {})
        queue_order = conveyor.get("queue_order", {})
        self._queue_order_axis = str(
            queue_order.get("axis", conveyor.get("image_flow_axis", "x"))
        ).lower()
        self._queue_order_direction = str(
            queue_order.get(
                "direction",
                conveyor.get("image_flow_direction", "left_to_right"),
            )
        ).lower()
        if self._queue_order_axis not in ("x", "y"):
            log.warning(
                "Invalid conveyor queue_order.axis=%r; falling back to 'x'",
                self._queue_order_axis,
            )
            self._queue_order_axis = "x"

        # _last_dets: cached for OVERLAY DRAWING only.
        # NEVER used to push into the detection queue on non-inference frames.
        # See module docstring for the full explanation.
        self._last_dets: list[dict] = []

    # ── Thread body ────────────────────────────────────────────────────────

    def run(self) -> None:
        try:
            self._load_model()
        except RuntimeError as e:
            log.critical("%s", e)
            self.stop_event.set()
            return

        try:
            cap = self._open_camera()
        except RuntimeError as e:
            log.critical("%s", e)
            self.stop_event.set()
            return

        capture = _CaptureThread(cap, self.stop_event)
        capture.start()

        # Wait for the buffer to fill before entering inference loop
        for _ in range(10):
            if capture.buffer:
                break
            time.sleep(0.01)

        cycle_times: deque = deque(maxlen=60)
        log.info("FruitDetector (T1) started — dual-thread mode")

        last_processed_frame_id = -1

        while not self.stop_event.is_set():
            t0 = time.monotonic()

            frame_data = capture.get_latest()
            if frame_data is None:
                time.sleep(0.001)
                continue

            # Unpack frame_id, frame and its capture timestamp
            capture_frame_id, frame, capture_ts_ms = frame_data

            # ── CRITICAL: Skip if we already processed this frame ──
            # Prevents duplicate inference when inference loop runs faster than camera FPS
            if capture_frame_id == last_processed_frame_id:
                time.sleep(0.005)  # Wait for new frame
                continue

            last_processed_frame_id = capture_frame_id

            self._frame_id     += 1
            self._skip_counter += 1

            # Publish JPEG frame to the event bus → flask_app streams it
            _, jpeg = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 70])
            bus.emit(EVT_FRAME, jpeg.tobytes())

            # ── Backpressure: skip inference if queue is near full ────────
            #
            # CRITICAL: deque with maxlen silently drops oldest items when
            # append() is called on a full queue. This breaks timing window
            # logic in SortController because:
            #   1. Queue holds 20 newest detections
            #   2. SortController processes from queue[0] (oldest)
            #   3. If queue overflows, oldest items are dropped
            #   4. IR trigger arrives → queue[0] is now much newer than expected
            #   5. Timing window check fails → fruit not sorted
            #
            # Solution: Stop inference when queue reaches high water mark.
            # This creates backpressure: if SortController is slow, we stop
            # detecting new fruits until queue drains.
            #
            # High water mark = 80% of maxlen (16 out of 20).
            # This leaves buffer for in-flight detections while preventing
            # silent drops.
            
            queue_size = len(self.queue)
            queue_maxlen = self.cfg["system"].get("queue_maxlen", 20)
            high_water_mark = int(queue_maxlen * 0.8)
            
            if queue_size >= high_water_mark:
                # Queue near full - skip inference to prevent overflow
                if self._frame_id % 30 == 0:  # Log every 30 frames (~1 sec)
                    log.warning(
                        "Queue backpressure: size=%d/%d, skipping inference",
                        queue_size, queue_maxlen
                    )
                continue  # Skip to next frame without inference

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
                # Enqueue detections ONLY on inference frames, in physical
                # downstream-first order instead of NMS/confidence order.
                for det in self._order_detections_for_queue(self._last_dets):
                    bus.emit(
                        EVT_DETECTION,
                        label=det["label"],
                        confidence=det["confidence"],
                    )

                    if not self._claim_new_object(det, capture_ts_ms):
                        continue

                    result = self._build_result(det, capture_ts_ms)
                    if result:
                        with self.lock:
                            self.queue.append(result)

            elapsed = time.monotonic() - t0
            cycle_times.append(elapsed)

            if self._frame_id % 300 == 0:
                avg_ms = (sum(cycle_times) / len(cycle_times)) * 1000
                log.info(
                    "Perception: fps=%.1f | cycle=%.1fms | queue=%d | cam_drop=%d",
                    1000 / avg_ms, avg_ms, len(self.queue), capture.drop_count,
                )

        cap.release()
        self._camera_opened = False
        log.info("FruitDetector stopped")

    # ── Model loading ──────────────────────────────────────────────────────

    def _load_model(self) -> None:
        if self._simulation_enabled:
            log.warning("Simulation mode enabled; NCNN model loading is skipped")
            self._interp = None
            self._model_loaded = True
            self._model_error = None
            return

        model_dir  = self.cfg["model"]["path"]
        n_threads  = self.cfg["model"].get("num_threads", 4)
        param_path = f"{model_dir}/model.ncnn.param"
        bin_path   = f"{model_dir}/model.ncnn.bin"
        log.info("Loading NCNN model: %s", model_dir)
        try:
            missing = [p for p in (param_path, bin_path) if not Path(p).is_file()]
            if missing:
                raise FileNotFoundError(f"Missing NCNN model file(s): {missing}")

            import ncnn
            self._interp = ncnn.Net()
            self._interp.opt.use_vulkan_compute = False
            self._interp.opt.num_threads        = n_threads
            param_status = self._interp.load_param(param_path)
            model_status = self._interp.load_model(bin_path)
            if param_status not in (0, None) or model_status not in (0, None):
                raise RuntimeError(
                    "NCNN loader returned "
                    f"load_param={param_status}, load_model={model_status}"
                )
            w, h = self.cfg["model"]["input_size"]
            self._input_wh = (int(w), int(h))
            self._model_loaded = True
            self._model_error = None
            log.info("NCNN ready | input=%s | threads=%d", self._input_wh, n_threads)
        except Exception as e:
            self._interp = None
            self._model_loaded = False
            self._model_error = str(e)
            raise RuntimeError(
                "Model load failed in "
                f"{self._system_mode!r} mode; simulation is disabled"
            ) from e

    # ── Camera open ────────────────────────────────────────────────────────

    def _open_camera(self) -> cv2.VideoCapture:
        cap = cv2.VideoCapture(self._cam_idx, cv2.CAP_V4L2)
        cap.set(cv2.CAP_PROP_FRAME_WIDTH,  self._cam_w)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self._cam_h)
        cap.set(cv2.CAP_PROP_FPS,          self._cam_fps)
        cap.set(cv2.CAP_PROP_BUFFERSIZE,   self._cam_buf)
        cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
        if not cap.isOpened():
            self._camera_opened = False
            self._camera_error = f"Cannot open camera {self._cam_idx}"
            raise RuntimeError(f"Cannot open camera {self._cam_idx}")
        self._camera_opened = True
        self._camera_error = None
        log.info(
            "Camera: %d×%d @ %.0ffps",
            self._cam_w, self._cam_h, cap.get(cv2.CAP_PROP_FPS),
        )
        return cap

    # ── Inference ──────────────────────────────────────────────────────────

    def _run_inference(self, frame: np.ndarray) -> list[dict]:
        if self._interp is None:
            if self._simulation_enabled:
                return self._simulate()
            log.error("Inference skipped: model is not loaded")
            return []

        import ncnn
        W, H = self._input_wh
        blob_img, (px, py, sc) = _letterbox(frame, W, H)

        # ── CRITICAL: Color space conversion ──────────────────────────────
        # OpenCV reads camera frames in BGR format (Blue-Green-Red).
        # YOLO models are trained on RGB images (Red-Green-Blue).
        # 
        # WRONG (current bug):
        #   ncnn.Mat.from_pixels(..., PIXEL_BGR, ...)
        #   → Feeds BGR directly to model
        #   → Model sees Red as Blue, Blue as Red
        #   → Red apple appears as dark blue/purple to the model
        #   → Color classification completely fails
        #
        # CORRECT:
        #   ncnn.Mat.from_pixels(..., PIXEL_BGR2RGB, ...)
        #   → NCNN automatically swaps B and R channels
        #   → Model receives correct RGB input
        #   → Color classification works as trained
        #
        # This MUST match the preprocessing in tools/test_model.py, which
        # correctly uses cv2.cvtColor(img, cv2.COLOR_BGR2RGB) before inference.

        mat_in = ncnn.Mat.from_pixels(
            blob_img, ncnn.Mat.PixelType.PIXEL_BGR2RGB, W, H
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

    def _build_result(self, det: dict, capture_ts_ms: float) -> Optional[DetectionResult]:
        """
        Build DetectionResult with the frame's capture timestamp.
        
        CRITICAL: capture_ts_ms is the timestamp from when the frame was
        captured by the camera, NOT when inference completed. This ensures
        accurate timing calculations in SortController's IR window logic.
        
        On RPi4, inference can take 50-150ms. Using post-inference timestamp
        would cause 4.5cm positional error at 0.3 m/s belt speed.
        """
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
            timestamp_ms=capture_ts_ms,  # Use capture time, not current time
        )

    def health_status(self) -> dict:
        """Return a thread-safe-enough snapshot for /api/health."""
        if self._model_loaded:
            model_status = "ok"
        elif self._model_error:
            model_status = "error"
        else:
            model_status = "starting"

        if self._camera_opened:
            camera_status = "ok"
        elif self._camera_error:
            camera_status = "error"
        else:
            camera_status = "starting"

        with self.lock:
            queue_depth = len(self.queue)

        return {
            "model": {
                "status": model_status,
                "loaded": self._model_loaded,
                "simulation": self._simulation_enabled,
                "error": self._model_error,
            },
            "camera_device": {
                "status": camera_status,
                "opened": self._camera_opened,
                "device_index": self._cam_idx,
                "error": self._camera_error,
            },
            "frames_processed": self._frame_id,
            "queue_depth": queue_depth,
            "active_tracks": len(self._tracks),
        }

    def _claim_new_object(self, det: dict, capture_ts_ms: float) -> bool:
        """
        Return True only once a physical object's label is stable.

        Inference runs on several frames while the same fruit remains in view.
        This lightweight tracker matches detections by bbox overlap/center
        movement and waits for two consecutive matching labels before
        allowing the detection to be enqueued.
        """
        self._expire_tracks(capture_ts_ms)

        bbox = det["bbox"]
        label = det["label"]
        track = self._match_track(bbox)
        if track is None:
            track = _ObjectTrack(
                track_id=self._next_track_id,
                label=label,
                label_streak=1,
                bbox=bbox,
                last_seen_frame_id=self._frame_id,
                last_seen_ts_ms=capture_ts_ms,
            )
            self._tracks[track.track_id] = track
            self._next_track_id += 1
        else:
            if track.label == label:
                track.label_streak += 1
            else:
                track.label = label
                track.label_streak = 1
            track.bbox = bbox
            track.last_seen_frame_id = self._frame_id
            track.last_seen_ts_ms = capture_ts_ms

        if track.enqueued:
            log.debug(
                "Skipping duplicate detection for track=%d label=%s bbox=%s",
                track.track_id, det["label"], bbox,
            )
            return False

        if track.label_streak < 2:
            return False

        track.enqueued = True
        return True

    def _expire_tracks(self, capture_ts_ms: float) -> None:
        expired = [
            track_id
            for track_id, track in self._tracks.items()
            if (capture_ts_ms - track.last_seen_ts_ms) > self._track_ttl_ms
        ]
        for track_id in expired:
            self._tracks.pop(track_id, None)

    def _match_track(self, bbox: tuple[int, int, int, int]) -> _ObjectTrack | None:
        best_track: _ObjectTrack | None = None
        best_score = -1.0

        for track in self._tracks.values():
            if track.last_seen_frame_id == self._frame_id:
                continue

            iou = _bbox_iou(bbox, track.bbox)
            dist = _center_distance(bbox, track.bbox)
            max_box_extent = max(bbox[2], bbox[3], track.bbox[2], track.bbox[3])
            dist_limit = max(self._track_center_dist_px, max_box_extent * 0.75)

            if iou < self._track_iou_thr and dist > dist_limit:
                continue

            score = iou + max(0.0, 1.0 - (dist / max(dist_limit, 1.0))) * 0.25
            if score > best_score:
                best_score = score
                best_track = track

        return best_track

    def _order_detections_for_queue(self, detections: list[dict]) -> list[dict]:
        """
        Sort same-frame detections by conveyor physics, downstream first.

        YOLO/NMS returns detections in confidence order. The control thread is
        FIFO, so the queue must represent belt order: fruit closest to the IR
        station must be consumed first.
        """
        if len(detections) <= 1:
            return list(detections)

        increasing_downstream = self._queue_order_direction in {
            "left_to_right",
            "top_to_bottom",
            "increasing",
            "positive",
            "right",
            "down",
        }

        def position(det: dict) -> float:
            cx, cy = _bbox_center(det["bbox"])
            return cx if self._queue_order_axis == "x" else cy

        return sorted(detections, key=position, reverse=increasing_downstream)

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
    direction = route.get("direction", "pass")

    if servo is None:
        # RED và UNKNOWN: không kích servo
        if direction == "pass":
            return SortAction.PASS     # ← ĐỔI
        return SortAction.REJECT

    key = f"SERVO{servo}_FIRE"         # ← ĐỔI: không còn LEFT/RIGHT
    return SortAction[key] if key in SortAction.__members__ else SortAction.REJECT
