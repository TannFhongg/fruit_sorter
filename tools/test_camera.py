"""
tools/test_camera.py
Test USB Camera — chụp 30 frame, đo FPS thực tế, lưu 1 ảnh để kiểm tra.

Chạy: python tools/test_camera.py
      python tools/test_camera.py --device 0 --frames 60
"""

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import cv2


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--device", type=int, default=0)
    parser.add_argument("--frames", type=int, default=30)
    parser.add_argument("--save",   default="logs/test_frame.jpg")
    args = parser.parse_args()

    print(f"Mở camera /dev/video{args.device}...")
    cap = cv2.VideoCapture(args.device, cv2.CAP_V4L2)
    cap.set(cv2.CAP_PROP_FRAME_WIDTH,  640)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
    cap.set(cv2.CAP_PROP_FPS,          30)
    cap.set(cv2.CAP_PROP_BUFFERSIZE,   2)
    cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))

    if not cap.isOpened():
        print("LỖI: Không mở được camera")
        sys.exit(1)

    actual_fps = cap.get(cv2.CAP_PROP_FPS)
    actual_w   = cap.get(cv2.CAP_PROP_FRAME_WIDTH)
    actual_h   = cap.get(cv2.CAP_PROP_FRAME_HEIGHT)
    print(f"Camera mở thành công: {actual_w:.0f}×{actual_h:.0f} @ {actual_fps:.0f}fps")

    # Warmup — bỏ 5 frame đầu
    for _ in range(5):
        cap.read()

    # Đo FPS thực tế
    print(f"Đọc {args.frames} frame...")
    t_start = time.monotonic()
    failed  = 0

    for i in range(args.frames):
        ret, frame = cap.read()
        if not ret:
            failed += 1
            continue

        # Lưu frame đầu tiên thành công
        if i == 0:
            Path(args.save).parent.mkdir(parents=True, exist_ok=True)
            cv2.imwrite(args.save, frame)
            h, w = frame.shape[:2]
            print(f"Frame shape: {w}×{h}×{frame.shape[2]}")
            print(f"Frame lưu tại: {args.save}")

    elapsed = time.monotonic() - t_start
    real_fps = (args.frames - failed) / elapsed

    print(f"\n── Kết quả ──────────────────────")
    print(f"  Frames đọc thành công : {args.frames - failed}/{args.frames}")
    print(f"  Thời gian             : {elapsed:.2f}s")
    print(f"  FPS thực tế           : {real_fps:.1f}")
    print(f"  Latency trung bình    : {elapsed/args.frames*1000:.1f}ms/frame")

    if failed > 0:
        print(f"  CẢNH BÁO: {failed} frame thất bại")
    if real_fps < 20:
        print(f"  CẢNH BÁO: FPS thấp hơn 20, kiểm tra lại USB bandwidth")
    else:
        print("  Camera: OK ✓")

    cap.release()


if __name__ == "__main__":
    main()