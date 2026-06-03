"""
drivers/servo/servo_driver.py
Helper tính lệnh servo từ config — không điều khiển GPIO trực tiếp.
Thực tế GPIO PWM nằm trên Arduino Slave (arduino_firmware.ino).
Module này chỉ cung cấp logic mapping angle + validation.

v3.0 — SWEEP mechanism
=======================
Trước đây (v2): servo giữ một góc cố định (home/fire) rồi về.
Bây giờ  (v3):  servo thực hiện cú QUÉT NHANH:
  - angle_home  (0°)  : vị trí nghỉ — cánh gạt song song băng chuyền
  - angle_sweep (120°): góc quét tối đa — cạnh cánh "tát" quả sang bên

Firmware (arduino_firmware.ino v3.0) tự quản lý hai pha:
  PHASE_SWEEPING  : 0 → 120° (sweep_duration_ms ≈ 200 ms)
  PHASE_RETURNING : 120° → 0° (return_duration_ms ≈ 300 ms)

RPi chỉ cần gửi {"cmd":"SORT","servo":1,"dir":"fire"} và nhận SORT_DONE.
"""

from __future__ import annotations
from dataclasses import dataclass


@dataclass
class ServoCommand:
    servo_id:          int
    angle:             int        # góc đích gửi cho Arduino (0 hoặc sweep)
    sweep_duration_ms: int = 200  # thời gian hoàn thành pha sweep (ms)
    return_duration_ms: int = 300 # thời gian trở về home (ms)
    direction:         str = ""   # "fire" | "home" — label debug


class ServoDriver:
    """
    Tạo ServoCommand từ config và action string.
    Không kết nối phần cứng — chỉ là logic layer.

    SWEEP design (v3.0):
      "fire" → angle_sweep (servo quét nhanh, đánh quả sang bên)
      "home" / "pass" / other → angle_home (về vị trí nghỉ)
    """

    def __init__(self, cfg: dict):
        self._servos = cfg["hardware"]["servos"]

    def build_command(self, servo_id: int, direction: str) -> ServoCommand:
        """
        Args:
            servo_id:  1 hoặc 2
            direction: "fire" | "home" | "pass"

        Returns:
            ServoCommand với angle phù hợp

        Raises:
            ValueError: nếu servo_id không tồn tại trong config
        """
        key = f"servo{servo_id}"
        srv = self._servos.get(key)
        if srv is None:
            raise ValueError(f"Unknown servo id: {servo_id}")

        sweep_ms  = srv.get("sweep_duration_ms",  200)
        return_ms = srv.get("return_duration_ms", 300)

        if direction == "fire":
            angle = srv["angle_sweep"]
        else:
            angle     = srv["angle_home"]
            direction = "home"  # normalise cho debug label

        return ServoCommand(
            servo_id=servo_id,
            angle=angle,
            sweep_duration_ms=sweep_ms,
            return_duration_ms=return_ms,
            direction=direction,
        )

    def home_all(self) -> list[ServoCommand]:
        """Trả về lệnh về home cho tất cả servo (dùng khi reset)."""
        cmds = []
        for i in (1, 2):
            key = f"servo{i}"
            srv = self._servos.get(key, {})
            cmds.append(ServoCommand(
                servo_id=i,
                angle=srv.get("angle_home", 0),
                sweep_duration_ms=srv.get("sweep_duration_ms", 200),
                return_duration_ms=srv.get("return_duration_ms", 300),
                direction="home",
            ))
        return cmds