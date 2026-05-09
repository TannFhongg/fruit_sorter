"""
perception/fruit_detector.py  ·  Thread 1 — Perception (HIGH-FPS VERSION)
=========================================================================
Kiến trúc 2 thread nội bộ:
  CaptureThread  : đọc camera liên tục vào FrameBuffer (deque maxlen=2)
  InferenceLoop  : lấy frame mới nhất từ buffer, chạy NCNN, push vào queue

Kết quả: camera không bao giờ chờ NCNN, NCNN không bao giờ chờ camera.
Target: 25-30 FPS trên RPi4 với input 320×320.
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
from web.flask_app import push_frame, push_detection_event  # ← THÊM

log = logging.getLogger(__name__)


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
        i = order[0]; keep.append(int(i))
        if order.size == 1: break
        inter = (
            np.maximum(0, np.minimum(x2[i], x2[order[1:]]) - np.maximum(x1[i], x1[order[1:]])) *
            np.maximum(0, np.minimum(y2[i], y2[order[1:]]) - np.maximum(y1[i], y1[order[1:]]))
        )
        iou = inter / (areas[i] + areas[order[1:]] - inter + 1e-6)
        order = order[1:][iou <= iou_thr]
    return keep


# ── Internal capture thread ────────────────────────────────────────────────

class _CaptureThread(threading.Thread):
    """
    Chạy nền, đọc camera liên tục vào frame_buffer.
    maxlen=2 đảm bảo InferenceLoop luôn lấy được frame mới nhất,
    không bao giờ xử lý frame cũ hơn 1 inference cycle.
    """

    def __init__(self, cap: cv2.VideoCapture, stop_event: threading.Event):
        super().__init__(name="CaptureThread", daemon=True)
        self._cap   = cap
        self._stop  = stop_event
        # maxlen=2: slot 0 = frame đang infer, slot 1 = frame tiếp theo sẵn sàng
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
                    self.drop_count += 1  # InferenceLoop đang chậm hơn camera
                self.buffer.append(frame)
            self.frame_count += 1
        log.info(f"CaptureThread stopped | captured={self.frame_count} dropped={self.drop_count}")


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
        self._conf_thr  = m["thresholds"]["confidence"]
        self._iou_thr   = m["thresholds"]["iou_nms"]
        self._min_area  = m["thresholds"]["min_bbox_area"]
        self._labels    = m["labels"]
        self._routing   = m["routing"]

        cam = cfg["camera"]
        self._cam_idx = cam["device_index"]
        self._cam_w   = cam["width"]
        self._cam_h   = cam["height"]
        self._cam_fps = cam["fps"]
        self._cam_buf = cam["buffer_size"]

        # Frame skip: chỉ infer mỗi N frame, giữ kết quả cũ cho frame bỏ qua
        self._skip_n       = cfg.get("model", {}).get("frame_skip", 2)
        self._skip_counter = 0
        self._last_dets: list[dict] = []

    def run(self) -> None:
        self._load_model()
        cap     = self._open_camera()
        capture = _CaptureThread(cap, self.stop_event)
        capture.start()

        # Warmup: chờ buffer có frame
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

            self._frame_id += 1
            self._skip_counter += 1

            # ── Push JPEG frame lên /video_feed ──────────────────────────
            _, jpeg = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 70])
            push_frame(jpeg.tobytes())

            # Frame skip: chỉ chạy NCNN mỗi _skip_n frame
            if self._skip_counter >= self._skip_n:
                self._skip_counter = 0
                self._last_dets = self._run_inference(frame)

            for det in self._last_dets:
                result = self._build_result(det)
                if result:
                    with self.lock:
                        self.queue.append(result)
                    # ── Push detection event lên dashboard ngay lập tức ──
                    push_detection_event(det["label"], det["confidence"])

            elapsed = time.monotonic() - t0
            cycle_times.append(elapsed)

            if self._frame_id % 300 == 0:
                avg_ms = (sum(cycle_times) / len(cycle_times)) * 1000
                log.info(
                    f"Perception: fps={1000/avg_ms:.1f} | "
                    f"cycle={avg_ms:.1f}ms | "
                    f"queue={len(self.queue)} | "
                    f"cam_drop={capture.drop_count}"
                )

        cap.release()
        log.info("FruitDetector stopped")

    def _load_model(self) -> None:
        import ncnn
        model_dir  = self.cfg["model"]["path"]
        n_threads  = self.cfg["model"].get("num_threads", 4)
        param_path = f"{model_dir}/model.ncnn.param"
        bin_path   = f"{model_dir}/model.ncnn.bin"
        log.info(f"Loading NCNN model: {model_dir}")
        try:
            self._interp = ncnn.Net()
            self._interp.opt.use_vulkan_compute = False
            self._interp.opt.num_threads        = n_threads
            self._interp.load_param(param_path)
            self._interp.load_model(bin_path)
            w, h = self.cfg["model"]["input_size"]
            self._input_wh = (int(w), int(h))
            log.info(f"NCNN ready | input={self._input_wh} | threads={n_threads}")
        except Exception as e:
            log.error(f"Model load failed: {e} → simulation mode")
            self._interp = None

    def _open_camera(self) -> cv2.VideoCapture:
        cap = cv2.VideoCapture(self._cam_idx, cv2.CAP_V4L2)
        cap.set(cv2.CAP_PROP_FRAME_WIDTH,  self._cam_w)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self._cam_h)
        cap.set(cv2.CAP_PROP_FPS,          self._cam_fps)
        cap.set(cv2.CAP_PROP_BUFFERSIZE,   self._cam_buf)
        cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
        if not cap.isOpened():
            raise RuntimeError(f"Cannot open camera {self._cam_idx}")
        log.info(f"Camera: {self._cam_w}×{self._cam_h} @ {cap.get(cv2.CAP_PROP_FPS):.0f}fps")
        return cap

    def _run_inference(self, frame: np.ndarray) -> list[dict]:
        if self._interp is None:
            return self._simulate()

        import ncnn
        W, H = self._input_wh
        blob_img, (px, py, sc) = _letterbox(frame, W, H)

        # Dùng PIXEL_BGR trực tiếp — bỏ cv2.cvtColor (tiết kiệm ~11ms)
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

    def _decode(self, raw, px, py, sc, orig_shape):
        out = raw[0]
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
        bxywh = bxywh[mask]; confs = confs[mask]; cids = cids[mask]
        keep  = _nms(bxywh, confs, self._iou_thr)
        oh, ow = orig_shape[:2]
        results = []
        for i in keep:
            cx, cy, bw, bh = bxywh[i]
            x0 = int((cx - px) / sc - bw / (2 * sc))
            y0 = int((cy - py) / sc - bh / (2 * sc))
            w  = int(bw / sc); h = int(bh / sc)
            x0, y0 = max(0, x0), max(0, y0)
            w = min(w, ow - x0); h = min(h, oh - y0)
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
        return [{"label": label, "confidence": round(random.uniform(0.70, 0.97), 2),
                 "bbox": (80, 55, 80, 80)}]


def _letterbox(img, tw, th):
    ih, iw = img.shape[:2]
    sc = min(tw / iw, th / ih)
    nw, nh = int(iw * sc), int(ih * sc)
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