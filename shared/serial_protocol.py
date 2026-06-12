"""
shared/serial_protocol.py
Giao thức UART JSON giữa RPi (Master) và Arduino (Slave).

v3.3 — SWEEP mechanism with dynamic 270° home/angle/timing
===========================================================
Master → Slave:
  {"cmd":"SORT","servo":1,"dir":"fire","angle":120,"home":0,"sweep_ms":200,"return_ms":300,"max":270,"min_us":500,"max_us":2500}
  {"cmd":"SORT","servo":1,"dir":"home","angle":0,"home":0,"sweep_ms":200,"return_ms":300,"max":270,"min_us":500,"max_us":2500}
  {"cmd":"PING"}
  {"cmd":"RESET","home1":0,"home2":0,"max":270,"min_us":500,"max_us":2500}
  {"cmd":"STATUS"}

Slave → Master:
  {"ack":"IR_TRIGGER","sensor":1,"ts":98234}
  {"ack":"SORT_DONE","servo":1,"angle":120,"total_ms":500}  ← sweep+return nominal time
  {"ack":"PONG","uptime_s":1234}
  {"ack":"STATUS","servo1_phase":0,"servo2_phase":0,...}
  {"ack":"ERROR","msg":"unknown_cmd"}

SORT_DONE.total_ms = sweep_ms + return_ms (nominal).
Servo is still physically moving when SORT_DONE is received;
Master treats it as "command accepted", not "servo idle".

ANGLE & TIMING SYNCHRONIZATION (v3.3):
  Raspberry Pi reads angle_home, angle_sweep, angle_max, pulse_min_us,
  pulse_max_us, sweep_duration_ms, and return_duration_ms from
  config/hardware_config.yaml and sends them in SORT/RESET commands.
  Arduino no longer uses hardcoded #define for runtime angles or timing.
  This ensures config changes on RPi take immediate effect without Arduino recompile.
  
  Physical consistency: If you increase angle_sweep (e.g., 120° → 180°),
  you must also increase sweep_duration_ms proportionally to give the servo
  enough time to complete the motion. Otherwise, the servo will be cut off
  mid-sweep and forced to return prematurely.
"""

from __future__ import annotations
import json
from typing import Optional


def cmd_sort(
    servo_id: int,
    direction: str,
    angle: int = 120,
    sweep_ms: int = 200,
    return_ms: int = 300,
    home_angle: int = 0,
    angle_max: int = 270,
    pulse_min_us: int = 500,
    pulse_max_us: int = 2500,
) -> bytes:
    """
    Build a SORT command.

    direction:
      "fire" → trigger the sweep (angle_home → angle_sweep → angle_home)
      "home" → immediately return to home (used by RESET flow)
    
    angle: sweep angle in physical degrees (default 120, read from config)
    home_angle: rest angle in physical degrees (default 0, read from config)
    angle_max: physical servo range in degrees (270 for common 270° servos)
    pulse_min_us/pulse_max_us: PWM range used to map physical angle to pulse
    sweep_ms: sweep phase duration in ms (default 200, read from config)
    return_ms: return phase duration in ms (default 300, read from config)
    """
    return _enc({
        "cmd": "SORT", 
        "servo": servo_id, 
        "dir": direction, 
        "angle": angle,
        "home": home_angle,
        "sweep_ms": sweep_ms,
        "return_ms": return_ms,
        "max": angle_max,
        "min_us": pulse_min_us,
        "max_us": pulse_max_us,
    })

def cmd_ping()   -> bytes: return _enc({"cmd": "PING"})
def cmd_reset(
    home_angles: dict[int, int] | None = None,
    angle_max: int = 270,
    pulse_min_us: int = 500,
    pulse_max_us: int = 2500,
) -> bytes:
    payload = {"cmd": "RESET"}
    if home_angles:
        payload["home1"] = home_angles.get(1, 0)
        payload["home2"] = home_angles.get(2, 0)
        payload["max"] = angle_max
        payload["min_us"] = pulse_min_us
        payload["max_us"] = pulse_max_us
    return _enc(payload)
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
