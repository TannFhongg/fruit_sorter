"""
shared/serial_protocol.py
Giao thức UART JSON giữa RPi (Master) và Arduino (Slave).

v3.1 — SWEEP mechanism with dynamic angle
==========================================
Master → Slave:
  {"cmd":"SORT","servo":1,"dir":"fire","angle":120}    ← kích cú quét với góc từ config
  {"cmd":"SORT","servo":1,"dir":"home","angle":0}      ← về vị trí nghỉ (reset)
  {"cmd":"PING"}
  {"cmd":"RESET"}
  {"cmd":"STATUS"}

Slave → Master:
  {"ack":"IR_TRIGGER","sensor":1,"ts":98234}
  {"ack":"SORT_DONE","servo":1,"angle":120,"total_ms":500}  ← sweep+return nominal time
  {"ack":"PONG","uptime_s":1234}
  {"ack":"STATUS","servo1_phase":0,"servo2_phase":0,...}
  {"ack":"ERROR","msg":"unknown_cmd"}

SORT_DONE.total_ms = SWEEP_DURATION_MS + RETURN_DURATION_MS (nominal).
Servo is still physically moving when SORT_DONE is received;
Master treats it as "command accepted", not "servo idle".

ANGLE SYNCHRONIZATION (v3.1):
  Raspberry Pi reads angle_sweep from config/hardware_config.yaml and sends
  it in every SORT command. Arduino no longer uses hardcoded #define for angle.
  This ensures config changes on RPi take immediate effect without Arduino recompile.
"""

from __future__ import annotations
import json
from typing import Optional


def cmd_sort(servo_id: int, direction: str, angle: int = 120) -> bytes:
    """
    Build a SORT command.

    direction:
      "fire" → trigger the sweep (0° → angle_sweep → 0°)
      "home" → immediately return to home (used by RESET flow)
    
    angle: sweep angle in degrees (default 120, read from config)
    """
    return _enc({"cmd": "SORT", "servo": servo_id, "dir": direction, "angle": angle})

def cmd_ping()   -> bytes: return _enc({"cmd": "PING"})
def cmd_reset()  -> bytes: return _enc({"cmd": "RESET"})
def cmd_status() -> bytes: return _enc({"cmd": "STATUS"})

def parse_response(raw: bytes) -> Optional[dict]:
    try:
        return json.loads(raw.decode("utf-8").strip())
    except Exception:
        return None

def is_ir_trigger(msg: dict) -> bool: return msg.get("ack") == "IR_TRIGGER"
def is_pong(msg: dict)       -> bool: return msg.get("ack") == "PONG"
def is_sort_done(msg: dict)  -> bool: return msg.get("ack") == "SORT_DONE"

def _enc(obj: dict) -> bytes:
    return (json.dumps(obj, separators=(",", ":")) + "\n").encode()