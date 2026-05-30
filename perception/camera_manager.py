"""
perception/camera_manager.py
Wrapper quản lý USB Camera qua OpenCV — tách biệt khỏi inference logic.
FruitDetector import CameraManager thay vì gọi cv2 trực tiếp.
"""

from __future__ import annotations

import logging
import time

import cv2
import numpy as np

log = logging.getLogger(__name__)


class CameraManager:
    """Context manager cho USB camera. Dùng với 'with' statement."""

    def __init__(self, cfg: dict):
        cam = cfg["camera"]
        self._idx = cam["device_index"]
        self._w   = cam["width"]
        self._h   = cam["height"]
        self._fps = cam["fps"]
        self._buf = cam["buffer_size"]
        self._cap: cv2.VideoCapture | None = None

    def __enter__(self) -> "CameraManager":
        self.open()
        return self

    def __exit__(self, *args):
        self.release()

    def open(self) -> None:
        self._cap = cv2.VideoCapture(self._idx, cv2.CAP_V4L2)
        self._cap.set(cv2.CAP_PROP_FRAME_WIDTH,  self._w)
        self._cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self._h)
        self._cap.set(cv2.CAP_PROP_FPS,          self._fps)
        self._cap.set(cv2.CAP_PROP_BUFFERSIZE,   self._buf)
        self._cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
        if not self._cap.isOpened():
            raise RuntimeError(f"Cannot open camera {self._idx}")
        actual_fps = self._cap.get(cv2.CAP_PROP_FPS)
        log.info(f"Camera opened: {self._w}×{self._h} @ {actual_fps:.0f}fps")

    def read(self) -> tuple[bool, np.ndarray | None]:
        if self._cap is None:
            return False, None
        return self._cap.read()

    def release(self) -> None:
        if self._cap:
            self._cap.release()
            self._cap = None
            log.info("Camera released")

    @property
    def is_open(self) -> bool:
        return self._cap is not None and self._cap.isOpened()

    def warmup(self, frames: int = 5) -> None:
        """Bỏ qua N frame đầu tiên (camera cần warmup trước khi ổn định màu sắc)."""
        for _ in range(frames):
            self._cap.read()
            time.sleep(0.03)
        log.debug(f"Camera warmup: {frames} frames dropped")