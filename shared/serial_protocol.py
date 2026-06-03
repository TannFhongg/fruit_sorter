"""
shared/serial_protocol.py
Giao thức UART JSON giữa RPi (Master) và Arduino (Slave).

v3.0 — SWEEP mechanism
=======================
Master → Slave:
  {"cmd":"SORT","servo":1,"dir":"fire"}    ← kích cú quét
  {"cmd":"SORT","servo":1,"dir":"home"}    ← về vị trí nghỉ (reset)
  {"cmd":"PING"}
  {"cmd":"RESET"}
  {"cmd":"STATUS"}

Slave → Master:
  {"ack":"IR_TRIGGER","sensor":1,"ts":98234}
  {"ack":"SORT_DONE","servo":1,"total_ms":500}  ← sweep+return nominal time
  {"ack":"PONG","uptime_s":1234}
  {"ack":"STATUS","servo1_phase":0,"servo2_phase":0,...}
  {"ack":"ERROR","msg":"unknown_cmd"}

SORT_DONE.total_ms = SWEEP_DURATION_MS + RETURN_DURATION_MS (nominal).
Servo is still physically moving when SORT_DONE is received;
Master treats it as "command accepted", not "servo idle".
"""

from __future__ import annotations
import json
from typing import Optional


def cmd_sort(servo_id: int, direction: str) -> bytes:
    """
    Build a SORT command.

    direction:
      "fire" → trigger the sweep (0° → angle_sweep → 0°)
      "home" → immediately return to home (used by RESET flow)
    """
    return _enc({"cmd": "SORT", "servo": servo_id, "dir": direction})

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