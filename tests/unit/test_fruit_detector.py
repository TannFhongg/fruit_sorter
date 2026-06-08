from __future__ import annotations

import threading
from collections import deque

import numpy as np
import pytest

from perception.fruit_detector import FruitDetector


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
                "GREEN": {"servo": 1, "direction": "fire"},
                "RED": {"servo": None, "direction": "pass"},
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
