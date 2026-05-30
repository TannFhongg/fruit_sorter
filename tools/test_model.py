"""
tools/test_model.py
Test NCNN model với 1 ảnh tĩnh — không cần camera.

Chạy: python tools/test_model.py --image logs/test_frame.jpg
"""

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import cv2
import ncnn
import numpy as np

from config.loader import load_config


def letterbox(img, tw, th):
    ih, iw = img.shape[:2]
    sc = min(tw / iw, th / ih)
    nw, nh = int(iw * sc), int(ih * sc)
    resized = cv2.resize(img, (nw, nh), interpolation=cv2.INTER_LINEAR)
    canvas = np.full((th, tw, 3), 114, dtype=np.uint8)
    px, py = (tw - nw) // 2, (th - nh) // 2
    canvas[py:py + nh, px:px + nw] = resized
    return canvas, (float(px), float(py), sc)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--image",  default="logs/test_frame.jpg")
    parser.add_argument("--config", default="config/hardware_config.yaml")
    args = parser.parse_args()

    cfg       = load_config(args.config)
    model_dir = cfg["model"]["path"]
    n_threads = cfg["model"].get("num_threads", 4)
    labels    = cfg["model"]["labels"]
    conf_thr  = cfg["model"]["thresholds"]["confidence"]
    W, H      = cfg["model"]["input_size"]

    # Load model
    print(f"Loading NCNN model từ: {model_dir}")
    net = ncnn.Net()
    net.opt.use_vulkan_compute = False
    net.opt.num_threads        = n_threads
    net.load_param(f"{model_dir}/model.ncnn.param")
    net.load_model(f"{model_dir}/model.ncnn.bin")
    print("Model loaded OK")

    # Load ảnh
    if not Path(args.image).exists():
        print(f"Ảnh không tồn tại: {args.image}")
        print("Chạy test_camera.py trước để chụp ảnh mẫu")
        sys.exit(1)

    frame = cv2.imread(args.image)
    print(f"Ảnh: {args.image} ({frame.shape[1]}×{frame.shape[0]})")

    # Inference
    blob_img, (px, py, sc) = letterbox(frame, W, H)
    blob_rgb = cv2.cvtColor(blob_img, cv2.COLOR_BGR2RGB)
    mat_in   = ncnn.Mat.from_pixels(blob_rgb, ncnn.Mat.PixelType.PIXEL_RGB, W, H)
    mat_in.substract_mean_normalize([0.0, 0.0, 0.0], [1/255.0]*3)

    ex = net.create_extractor()

    print("\nThử blob names:")
    for blob_in, blob_out in [("in0","out0"), ("images","output0"), ("input","output")]:
        try:
            r_in  = ex.input(blob_in, mat_in)
            r_out, mat_out = ex.extract(blob_out)
            if r_in == 0 and r_out == 0:
                print(f"  Blob name đúng: input='{blob_in}' output='{blob_out}' ✓")
                raw = np.array(mat_out)
                print(f"  Output shape: {raw.shape}")
                break
            ex = net.create_extractor()  # reset
        except Exception as e:
            print(f"  '{blob_in}'/'{blob_out}': {e}")
            ex = net.create_extractor()

    # Đo thời gian inference
    print("\nĐo inference time (10 lần)...")
    times = []
    for _ in range(10):
        t0 = time.monotonic()
        ex2 = net.create_extractor()
        ex2.input("in0", mat_in)
        ex2.extract("out0")
        times.append((time.monotonic() - t0) * 1000)

    avg_ms = sum(times) / len(times)
    print(f"  Inference avg: {avg_ms:.1f}ms  ({1000/avg_ms:.1f} FPS tối đa)")

    if avg_ms < 100:
        print("  Model speed: OK ✓")
    elif avg_ms < 200:
        print("  Model speed: Chấp nhận được (< 200ms)")
    else:
        print(f"  CẢNH BÁO: Inference chậm ({avg_ms:.0f}ms) — thử giảm num_threads hoặc input_size")


if __name__ == "__main__":
    main()
    