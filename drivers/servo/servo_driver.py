"""
drivers/servo/servo_driver.py
Helper tính lệnh servo từ config — không điều khiển GPIO trực tiếp.
Thực tế GPIO PWM nằm trên Arduino Slave (arduino_firmware.ino).
Module này chỉ cung cấp logic mapping angle + validation.

UPDATED: Simplified to match new 2-position design (home/fire only).
Old 3-position design (left/right/neutral) is deprecated.
"""

from __future__ import annotations
from dataclasses import dataclass


@dataclass
class ServoCommand:
    servo_id:  int
    angle:     int        # độ (0–180)
    hold_ms:   int = 150  # giữ góc bao lâu trước khi về home
    direction: str = ""   # "fire" | "home" — label debug


class ServoDriver:
    """
    Tạo ServoCommand từ config và action string.
    Không kết nối phần cứng — chỉ là logic layer.
    
    DESIGN: 2-position servo (home/fire)
      - home: resting position (angle_home from config)
      - fire: sorting position (angle_fire from config)
    """

    def __init__(self, cfg: dict):
        self._servos = cfg["hardware"]["servos"]

    def build_command(self, servo_id: int, direction: str) -> ServoCommand:
        """
        Args:
            servo_id:  1 hoặc 2
            direction: "fire" | "home" | "pass" (pass = home)
        
        Returns:
            ServoCommand with angle from config
        
        Raises:
            ValueError: if servo_id not found in config
        """
        key = f"servo{servo_id}"
        srv = self._servos.get(key)
        if srv is None:
            raise ValueError(f"Unknown servo id: {servo_id}")

        # Map direction to config keys
        # "fire" → angle_fire
        # "home", "pass", or anything else → angle_home
        if direction == "fire":
            angle = srv["angle_fire"]
        else:
            angle = srv["angle_home"]
            direction = "home"  # Normalize for debug label

        return ServoCommand(
            servo_id=servo_id,
            angle=angle,
            hold_ms=srv.get("hold_ms", 150),
            direction=direction,
        )

    def home_all(self) -> list[ServoCommand]:
        """Trả về lệnh về home cho tất cả servo (dùng khi reset)."""
        return [
            ServoCommand(
                servo_id=1, 
                angle=self._servos["servo1"]["angle_home"], 
                hold_ms=self._servos["servo1"].get("hold_ms", 150),
                direction="home"
            ),
            ServoCommand(
                servo_id=2, 
                angle=self._servos["servo2"]["angle_home"], 
                hold_ms=self._servos["servo2"].get("hold_ms", 150),
                direction="home"
            ),
        ]