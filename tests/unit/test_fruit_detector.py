from __future__ import annotations

import threading
from collections import deque

import numpy as np
import pytest

from perception.fruit_detector import FruitDetector
from shared.detection_result import FruitColor, SortAction


def _cfg(mode: str = "production") -> dict:
    return {
        "system": {"mode": mode, "queue_maxlen": 20},
        "camera": {
            "device_index": 0,
            "width": 640,
            "height": 480,
            "fps": 30,
            "buffer_size": 2,
        },
        "model": {
            "path": "models/does-not-exist",
            "type": "ncnn",
            "input_size": [320, 320],
            "num_threads": 1,
            "frame_skip": 1,
            "thresholds": {
                "confidence": 0.65,
                "iou_nms": 0.45,
                "min_bbox_area": 1500,
            },
            "labels": {0: "GREEN", 1: "RED", 2: "YELLOW"},
            "routing": {
                "GREEN": {"servo": None, "direction": "pass"},
                "RED": {"servo": 1, "direction": "fire"},
                "YELLOW": {"servo": 2, "direction": "fire"},
                "UNKNOWN": {"servo": None, "direction": "pass"},
            },
        },
    }


def _detector(mode: str = "production") -> FruitDetector:
    return FruitDetector(
        cfg=_cfg(mode),
        detection_queue=deque(maxlen=20),
        queue_lock=threading.Lock(),
        stop_event=threading.Event(),
    )


def test_model_load_failure_fails_closed_in_production():
    detector = _detector("production")

    with pytest.raises(RuntimeError, match="simulation is disabled"):
        detector._load_model()

    frame = np.zeros((64, 64, 3), dtype=np.uint8)
    assert detector._run_inference(frame) == []


def test_simulation_requires_explicit_simulation_mode():
    detector = _detector("simulation")
    frame = np.zeros((64, 64, 3), dtype=np.uint8)

    detector._simulate = lambda: [{"label": "GREEN", "confidence": 0.8, "bbox": (0, 0, 10, 10)}]

    assert detector._run_inference(frame) == [
        {"label": "GREEN", "confidence": 0.8, "bbox": (0, 0, 10, 10)}
    ]


def test_same_object_is_claimed_once_across_inference_frames():
    detector = _detector("simulation")
    det = {"label": "GREEN", "confidence": 0.90, "bbox": (100, 120, 80, 80)}

    detector._frame_id = 1
    assert detector._claim_new_object(det, capture_ts_ms=1000.0) is False

    detector._frame_id = 2
    moved_same_fruit = {
        "label": "GREEN",
        "confidence": 0.88,
        "bbox": (108, 122, 80, 80),
    }
    assert detector._claim_new_object(moved_same_fruit, capture_ts_ms=1100.0) is True

    detector._frame_id = 3
    moved_after_enqueue = {
        "label": "GREEN",
        "confidence": 0.87,
        "bbox": (116, 124, 80, 80),
    }
    assert detector._claim_new_object(moved_after_enqueue, capture_ts_ms=1200.0) is False


def test_label_must_be_stable_before_detection_is_enqueued():
    detector = _detector("simulation")

    def claim_and_enqueue(det: dict, capture_ts_ms: float) -> bool:
        if not detector._claim_new_object(det, capture_ts_ms):
            return False

        result = detector._build_result(det, capture_ts_ms)
        assert result is not None
        with detector.lock:
            detector.queue.append(result)
        return True

    detector._frame_id = 1
    green_first_frame = {
        "label": "GREEN",
        "confidence": 0.80,
        "bbox": (100, 120, 80, 80),
    }
    assert claim_and_enqueue(green_first_frame, capture_ts_ms=1000.0) is False
    assert list(detector.queue) == []

    detector._frame_id = 2
    red_first_frame = {
        "label": "RED",
        "confidence": 0.92,
        "bbox": (106, 122, 80, 80),
    }
    assert claim_and_enqueue(red_first_frame, capture_ts_ms=1100.0) is False
    assert list(detector.queue) == []

    detector._frame_id = 3
    red_second_frame = {
        "label": "RED",
        "confidence": 0.93,
        "bbox": (112, 124, 80, 80),
    }
    assert claim_and_enqueue(red_second_frame, capture_ts_ms=1200.0) is True
    assert len(detector.queue) == 1
    result = detector.queue[0]
    assert result.fruit_color == FruitColor.RED
    assert result.action == SortAction.SERVO1_FIRE
    assert result.confidence == 0.93
    assert result.timestamp_ms == 1200.0
    assert all(item.fruit_color != FruitColor.GREEN for item in detector.queue)
    assert all(item.action != SortAction.PASS for item in detector.queue)

    detector._frame_id = 4
    red_after_enqueue = {
        "label": "RED",
        "confidence": 0.94,
        "bbox": (118, 126, 80, 80),
    }
    assert claim_and_enqueue(red_after_enqueue, capture_ts_ms=1300.0) is False
    assert len(detector.queue) == 1


def test_same_frame_detections_are_ordered_downstream_first():
    detector = _detector("simulation")
    detections = [
        {"label": "GREEN", "confidence": 0.95, "bbox": (40, 50, 50, 50)},
        {"label": "YELLOW", "confidence": 0.70, "bbox": (250, 50, 50, 50)},
        {"label": "RED", "confidence": 0.85, "bbox": (150, 50, 50, 50)},
    ]

    ordered = detector._order_detections_for_queue(detections)

    assert [d["label"] for d in ordered] == ["YELLOW", "RED", "GREEN"]
