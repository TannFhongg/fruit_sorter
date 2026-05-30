"""
drivers/ir_sensor/ir_driver.py
Tài liệu và constants cho IR sensor — debounce logic chạy trên Arduino.

NOTE: IR sensor KHÔNG đọc trực tiếp từ RPi GPIO trong kiến trúc này.
Arduino Slave đọc IR interrupt và gửi IR_TRIGGER event lên RPi qua UART.
File này chứa constants và helper cho phía RPi để parse event đó.
"""

from __future__ import annotations

# Mapping sensor_id → config key
SENSOR_CONFIG_KEYS = {
    1: "ir1",
    2: "ir2",
}


def get_sensor_label(cfg: dict, sensor_id: int) -> str:
    key = SENSOR_CONFIG_KEYS.get(sensor_id, "")
    return cfg.get("hardware", {}).get("ir_sensors", {}).get(key, {}).get("label", f"sensor_{sensor_id}")


def get_debounce_ms(cfg: dict, sensor_id: int) -> int:
    key = SENSOR_CONFIG_KEYS.get(sensor_id, "")
    return cfg.get("hardware", {}).get("ir_sensors", {}).get(key, {}).get("debounce_ms", 20)


# Arduino pin assignments (chỉ để tham khảo — config canonical là hardware_config.yaml)
ARDUINO_IR_PINS = {1: 2, 2: 3}  # sensor_id → Arduino digital pin