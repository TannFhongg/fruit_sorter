"""
drivers/servo/servo_driver.py
Helper tính lệnh servo từ config — không điều khiển GPIO trực tiếp.
Thực tế GPIO PWM nằm trên Arduino Slave (main.ino).
Module này chỉ cung cấp logic mapping angle + validation.
"""

from __future__ import annotations
from dataclasses import dataclass


@dataclass
class ServoCommand:
    servo_id:  int
    angle:     int        # độ (0–180)
    hold_ms:   int = 500  # giữ góc bao lâu trước khi về neutral
    direction: str = ""   # "left" | "right" | "neutral" — label debug


class ServoDriver:
    """
    Tạo ServoCommand từ config và action string.
    Không kết nối phần cứng — chỉ là logic layer.
    """

    def __init__(self, cfg: dict):
        self._servos = cfg["hardware"]["servos"]

    def build_command(self, servo_id: int, direction: str) -> ServoCommand:
        """
        Args:
            servo_id:  1 hoặc 2
            direction: "left" | "right" | "neutral"
        """
        key = f"servo{servo_id}"
        srv = self._servos.get(key)
        if srv is None:
            raise ValueError(f"Unknown servo id: {servo_id}")

        angle_map = {
            "left":    srv["angle_left"],
            "right":   srv["angle_right"],
            "neutral": srv["angle_neutral"],
        }
        angle = angle_map.get(direction, srv["angle_neutral"])

        return ServoCommand(
            servo_id=servo_id,
            angle=angle,
            hold_ms=srv.get("hold_ms", 500),
            direction=direction,
        )

    def neutral_all(self) -> list[ServoCommand]:
        """Trả về lệnh về neutral cho tất cả servo (dùng khi reset)."""
        return [
            ServoCommand(servo_id=1, angle=self._servos["servo1"]["angle_neutral"], direction="neutral"),
            ServoCommand(servo_id=2, angle=self._servos["servo2"]["angle_neutral"], direction="neutral"),
        ]