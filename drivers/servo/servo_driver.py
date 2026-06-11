"""
drivers/servo/servo_driver.py
Helper tính lệnh servo từ config — không điều khiển GPIO trực tiếp.
Thực tế GPIO PWM nằm trên Arduino Slave (arduino_firmware.ino).
Module này chỉ cung cấp logic mapping angle + validation.

v3.3 — 270° SWEEP mechanism
============================
Trước đây (v2): servo giữ một góc cố định (home/fire) rồi về.
Bây giờ  (v3):  servo thực hiện cú QUÉT NHANH:
  - angle_home       : vị trí nghỉ — cánh gạt song song băng chuyền
  - angle_sweep      : góc quét tối đa — cạnh cánh "tát" quả sang bên
  - angle_max        : dải góc vật lý của servo, ví dụ 270° servo

Firmware (arduino_firmware.ino v3.3) tự quản lý hai pha:
  PHASE_SWEEPING  : angle_home → angle_sweep
  PHASE_RETURNING : angle_sweep → angle_home

RPi chỉ cần gửi {"cmd":"SORT","servo":1,"dir":"fire"} và nhận SORT_DONE.
"""

from __future__ import annotations
from dataclasses import dataclass


@dataclass
class ServoCommand:
    servo_id:          int
    angle:             int        # góc đích gửi cho Arduino (0 hoặc sweep)
    home_angle:        int = 0    # góc nghỉ vật lý
    angle_max:         int = 270  # dải góc vật lý của servo
    pulse_min_us:      int = 500
    pulse_max_us:      int = 2500
    sweep_duration_ms: int = 200  # thời gian hoàn thành pha sweep (ms)
    return_duration_ms: int = 300 # thời gian trở về home (ms)
    direction:         str = ""   # "fire" | "home" — label debug


class ServoDriver:
    """
    Tạo ServoCommand từ config và action string.
    Không kết nối phần cứng — chỉ là logic layer.

    SWEEP design:
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
        home_angle = srv.get("angle_home", 0)
        angle_max = srv.get("angle_max", 270)
        pulse_min_us = srv.get("pulse_min_us", 500)
        pulse_max_us = srv.get("pulse_max_us", 2500)

        if direction == "fire":
            angle = srv["angle_sweep"]
        else:
            angle     = home_angle
            direction = "home"  # normalise cho debug label

        return ServoCommand(
            servo_id=servo_id,
            angle=angle,
            home_angle=home_angle,
            angle_max=angle_max,
            pulse_min_us=pulse_min_us,
            pulse_max_us=pulse_max_us,
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
                home_angle=srv.get("angle_home", 0),
                angle_max=srv.get("angle_max", 270),
                pulse_min_us=srv.get("pulse_min_us", 500),
                pulse_max_us=srv.get("pulse_max_us", 2500),
                sweep_duration_ms=srv.get("sweep_duration_ms", 200),
                return_duration_ms=srv.get("return_duration_ms", 300),
                direction="home",
            ))
        return cmds
