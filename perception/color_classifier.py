"""
perception/color_classifier.py
Phân loại màu sắc bằng HSV — dùng làm fallback khi model YOLO
confidence thấp, hoặc để validate kết quả YOLO.
"""

from __future__ import annotations
import cv2
import numpy as np

_COLOR_RANGES = {
    "GREEN":  [(35,  50,  50), (85,  255, 255)],
    "RED1":   [(0,   100, 100), (10,  255, 255)],
    "RED2":   [(160, 100, 100), (179, 255, 255)],
    "YELLOW": [(20,  100, 100), (34,  255, 255)],
}
_MIN_PIXEL_RATIO = 0.15


def classify_color(frame: np.ndarray, bbox: tuple) -> str | None:
    x, y, w, h = bbox
    roi = frame[y:y + h, x:x + w]
    if roi.size == 0:
        return None

    hsv    = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
    total  = roi.shape[0] * roi.shape[1]
    scores: dict[str, float] = {}

    for color, (lo, hi) in _COLOR_RANGES.items():
        mask  = cv2.inRange(hsv, np.array(lo), np.array(hi))
        ratio = cv2.countNonZero(mask) / total
        if color == "RED1":
            scores["RED"] = scores.get("RED", 0) + ratio
        elif color == "RED2":
            scores["RED"] = scores.get("RED", 0) + ratio
        else:
            scores[color] = ratio

    if not scores:
        return None
    best_color = max(scores, key=scores.get)
    return best_color if scores[best_color] >= _MIN_PIXEL_RATIO else None


def validate_yolo_result(
    frame: np.ndarray,
    bbox: tuple,
    yolo_label: str,
    yolo_conf: float,
    conf_threshold: float = 0.65,
) -> str:
    if yolo_conf >= conf_threshold:
        return yolo_label
    hsv_label = classify_color(frame, bbox)
    if hsv_label and hsv_label == yolo_label:
        return yolo_label
    return hsv_label or "UNKNOWN"