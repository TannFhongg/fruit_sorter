/*
 * arduino_firmware/arduino_firmware.ino
 * =====================================================================
 * FruitSorter — Arduino Slave Firmware  v3.3 (270° SWEEP mechanism)
 * =====================================================================
 * Role   : Slave — receives commands from the Raspberry Pi (Master)
 *          via UART Serial.
 *
 * Tasks:
 *   1. Read 2× IR sensors using hardware interrupts (pins 2, 3)
 *   2. Send IR_TRIGGER events to Master as soon as a sensor fires
 *   3. Receive SORT commands from Master → actuate the correct servo
 *      using SWEEP (angle_home → angle_sweep → angle_home)
 *   4. Reply PONG to PING (heartbeat watchdog)
 *   5. Report STATUS on demand
 *
 * Protocol: JSON one-liner + '\n' @ 115200 baud
 *
 * Example commands:
 *   {"cmd":"SORT","servo":1,"dir":"fire","angle":0,"home":0,"max":270,"min_us":500,"max_us":2500,"sweep_ms":200,"return_ms":300}
 *   {"cmd":"SORT","servo":1,"dir":"home","angle":0,"home":0,"max":270,"min_us":500,"max_us":2500,"sweep_ms":200,"return_ms":300}
 *   {"cmd":"PING"}
 *   {"cmd":"RESET","home1":0,"home2":0,"max":270,"min_us":500,"max_us":2500}
 *   {"cmd":"STATUS"}
 *
 * =====================================================================
 * SWEEP mechanism (v3.3)
 * =====================================================================
 * PREVIOUS design ("push"): servo held a fixed angle and waited for
 * fruit to fall/slide off. Timing-sensitive, position-dependent.
 *
 * NEW design ("sweep" / flap):
 *   1. Flap rests at angle_home (read from JSON; servo1 defaults to 0°).
 *   2. On SORT command: servo sweeps angle_home → angle_sweep
 *      in SWEEP_DURATION_MS (~200 ms). The EDGE of the flap "slaps"
 *      the fruit sideways as it passes through the station.
 *   3. After sweep completes, servo returns to angle_home at a
 *      slightly slower pace (RETURN_DURATION_MS ~300 ms) to avoid
 *      hitting any fruit still on the belt.
 *
 * IMPORTANT: Home angle, sweep angle, timing, servo physical range, and PWM
 * calibration are now READ FROM JSON sent by Raspberry Pi, which reads them
 * from config/hardware_config.yaml.
 *
 * 270° servos:
 *   Arduino Servo.write(angle) treats values as 0..180 degrees and clamps
 *   Servo.write(220) to 180. This firmware therefore maps physical degrees
 *   (0..270) to PWM pulses and uses writeMicroseconds().
 *
 * Why MG996R works well here:
 *   - Stall torque 9–11 kg·cm @ 6V → ample force for a fast sweep
 *   - No-load speed ~0.14 s/60° @ 6V → 120° sweep in ~280 ms max
 *   - Software-timed sweep is accurate enough (±10 ms on AVR timer)
 *
 * SMOOTH SWEEP via intermediate positions:
 *   write_servo_angle() gives a step function — instant target pulse update.
 *   For the sweep to "hit" the fruit at maximum angular velocity,
 *   we WANT a fast sweep, so we rely on the servo's own slew rate.
 *   We write the target angle immediately; the servo accelerates
 *   on its own. No intermediate positions needed.
 *
 *   Sequence (non-blocking, managed by check_servo_state()):
 *     Phase IDLE    : servo at HOME
 *     Phase SWEEPING: write_servo_angle(SWEEP_ANGLE) issued; wait SWEEP_DURATION_MS
 *     Phase RETURNING: write_servo_home() issued; wait RETURN_DURATION_MS
 *     → back to IDLE
 *
 * =====================================================================
 * ISR-safety (unchanged from v2)
 * =====================================================================
 * ISRs only set a volatile bool flag — no millis(), no Serial.
 * Debounce and event emission both live in loop().
 *
 * =====================================================================
 * Non-blocking timing (unchanged from v2, extended for sweep phases)
 * =====================================================================
 * Uses millis() - start_ms >= duration pattern (overflow-safe).
 * Two phases per servo instead of one:
 *   Phase 1: SWEEPING  (duration = SWEEP_DURATION_MS)
 *   Phase 2: RETURNING (duration = RETURN_DURATION_MS)
 *
 * =====================================================================
 */

#include <Servo.h>
#include <ArduinoJson.h>

// ── Pin definitions ───────────────────────────────────────────────────────
#define PIN_IR1         2
#define PIN_IR2         3
#define PIN_SERVO1      9
#define PIN_SERVO2      10
#define PIN_STATUS_LED  13

// ── Servo angles / PWM calibration ────────────────────────────────────────
// Runtime values are updated from JSON commands sent by Raspberry Pi.
// Defaults are used at boot before the first command arrives.
#define SERVO1_DEFAULT_HOME_ANGLE  0
#define SERVO2_DEFAULT_HOME_ANGLE  0
#define DEFAULT_SERVO_MAX_ANGLE    270
#define DEFAULT_PULSE_MIN_US       500
#define DEFAULT_PULSE_MAX_US       2500
// SERVO_SWEEP_ANGLE is dynamic — read from incoming JSON command.

// ── Timing ────────────────────────────────────────────────────────────────
// SWEEP_DURATION_MS and RETURN_DURATION_MS are now DYNAMIC — read from JSON.
// Defaults below are only used as fallback if not specified in command.
#define DEFAULT_SWEEP_DURATION_MS   200
#define DEFAULT_RETURN_DURATION_MS  300

#define DEBOUNCE_MS         20    // minimum ms between two valid IR triggers
#define SERIAL_BAUD      115200

// ── Servo phase state ─────────────────────────────────────────────────────
// Each servo cycles through: IDLE → SWEEPING → RETURNING → IDLE
typedef enum {
  PHASE_IDLE      = 0,
  PHASE_SWEEPING  = 1,
  PHASE_RETURNING = 2,
} ServoPhase;

// ── ISR state ─────────────────────────────────────────────────────────────
volatile bool ir1_pending = false;
volatile bool ir2_pending = false;

// ── Debounce state ────────────────────────────────────────────────────────
uint32_t last_ir1_ms = 0;
uint32_t last_ir2_ms = 0;

// ── Servo state ───────────────────────────────────────────────────────────
Servo    servo1, servo2;

ServoPhase servo1_phase    = PHASE_IDLE;
ServoPhase servo2_phase    = PHASE_IDLE;
uint32_t   servo1_phase_start_ms = 0;
uint32_t   servo2_phase_start_ms = 0;

// Dynamic timing per servo (read from incoming JSON command)
uint16_t   servo1_sweep_duration_ms  = DEFAULT_SWEEP_DURATION_MS;
uint16_t   servo1_return_duration_ms = DEFAULT_RETURN_DURATION_MS;
uint16_t   servo2_sweep_duration_ms  = DEFAULT_SWEEP_DURATION_MS;
uint16_t   servo2_return_duration_ms = DEFAULT_RETURN_DURATION_MS;

// Dynamic home/range/PWM config per servo.
uint16_t   servo1_home_angle = SERVO1_DEFAULT_HOME_ANGLE;
uint16_t   servo2_home_angle = SERVO2_DEFAULT_HOME_ANGLE;
uint16_t   servo1_max_angle  = DEFAULT_SERVO_MAX_ANGLE;
uint16_t   servo2_max_angle  = DEFAULT_SERVO_MAX_ANGLE;
uint16_t   servo1_pulse_min_us = DEFAULT_PULSE_MIN_US;
uint16_t   servo1_pulse_max_us = DEFAULT_PULSE_MAX_US;
uint16_t   servo2_pulse_min_us = DEFAULT_PULSE_MIN_US;
uint16_t   servo2_pulse_max_us = DEFAULT_PULSE_MAX_US;

// ── Other state ───────────────────────────────────────────────────────────
uint32_t boot_ms = 0;

// ── Serial buffer ─────────────────────────────────────────────────────────
char     serial_buffer[256];
uint8_t  serial_buf_index = 0;

// ── ISRs ──────────────────────────────────────────────────────────────────
void isr_ir1() { ir1_pending = true; }
void isr_ir2() { ir2_pending = true; }

// ── Servo angle helpers ───────────────────────────────────────────────────
uint16_t angle_to_pulse_us(int angle, uint16_t max_angle,
                           uint16_t pulse_min_us, uint16_t pulse_max_us) {
  if (max_angle == 0) {
    max_angle = DEFAULT_SERVO_MAX_ANGLE;
  }
  if (pulse_max_us <= pulse_min_us) {
    pulse_min_us = DEFAULT_PULSE_MIN_US;
    pulse_max_us = DEFAULT_PULSE_MAX_US;
  }

  if (angle < 0) {
    angle = 0;
  }
  if (angle > (int)max_angle) {
    angle = max_angle;
  }

  long span = (long)pulse_max_us - (long)pulse_min_us;
  long pulse = (long)pulse_min_us + ((long)angle * span) / (long)max_angle;
  if (pulse < pulse_min_us) {
    pulse = pulse_min_us;
  }
  if (pulse > pulse_max_us) {
    pulse = pulse_max_us;
  }
  return (uint16_t)pulse;
}

void write_servo_angle(Servo& srv, int angle, uint16_t max_angle,
                       uint16_t pulse_min_us, uint16_t pulse_max_us) {
  srv.writeMicroseconds(
    angle_to_pulse_us(angle, max_angle, pulse_min_us, pulse_max_us)
  );
}

void write_servo_home(uint8_t id) {
  if (id == 1) {
    write_servo_angle(
      servo1, servo1_home_angle, servo1_max_angle,
      servo1_pulse_min_us, servo1_pulse_max_us
    );
  } else if (id == 2) {
    write_servo_angle(
      servo2, servo2_home_angle, servo2_max_angle,
      servo2_pulse_min_us, servo2_pulse_max_us
    );
  }
}

void store_servo_config(uint8_t id, int home_angle, int max_angle,
                        int pulse_min_us, int pulse_max_us) {
  if (max_angle <= 0) {
    max_angle = DEFAULT_SERVO_MAX_ANGLE;
  }
  if (pulse_max_us <= pulse_min_us) {
    pulse_min_us = DEFAULT_PULSE_MIN_US;
    pulse_max_us = DEFAULT_PULSE_MAX_US;
  }

  if (home_angle < 0) {
    home_angle = 0;
  }
  if (home_angle > max_angle) {
    home_angle = max_angle;
  }

  if (id == 1) {
    servo1_home_angle = home_angle;
    servo1_max_angle = max_angle;
    servo1_pulse_min_us = pulse_min_us;
    servo1_pulse_max_us = pulse_max_us;
  } else if (id == 2) {
    servo2_home_angle = home_angle;
    servo2_max_angle = max_angle;
    servo2_pulse_min_us = pulse_min_us;
    servo2_pulse_max_us = pulse_max_us;
  }
}

// ── Setup ─────────────────────────────────────────────────────────────────
void setup() {
  Serial.begin(SERIAL_BAUD);
  while (!Serial) { ; }

  servo1.attach(PIN_SERVO1, DEFAULT_PULSE_MIN_US, DEFAULT_PULSE_MAX_US);
  servo2.attach(PIN_SERVO2, DEFAULT_PULSE_MIN_US, DEFAULT_PULSE_MAX_US);
  write_servo_home(1);
  write_servo_home(2);

  pinMode(PIN_IR1, INPUT_PULLUP);
  pinMode(PIN_IR2, INPUT_PULLUP);
  attachInterrupt(digitalPinToInterrupt(PIN_IR1), isr_ir1, FALLING);
  attachInterrupt(digitalPinToInterrupt(PIN_IR2), isr_ir2, FALLING);

  pinMode(PIN_STATUS_LED, OUTPUT);
  digitalWrite(PIN_STATUS_LED, LOW);

  boot_ms = millis();

  StaticJsonDocument<64> doc;
  doc["boot"]     = "ok";
  doc["firmware"] = "FruitSorter-v3.3-270-sweep";
  serializeJson(doc, Serial);
  Serial.println();

  // Startup blink
  for (int i = 0; i < 3; i++) {
    digitalWrite(PIN_STATUS_LED, HIGH); delay(100);
    digitalWrite(PIN_STATUS_LED, LOW);  delay(100);
  }
}

// ── Non-blocking servo state machine ──────────────────────────────────────
//
// Called at the TOP of every loop() iteration.
//
// SWEEP state machine per servo:
//
//   IDLE:
//     Servo is at HOME. Waiting for actuate_servo() to start a sweep.
//
//   SWEEPING:
//     write_servo_angle(SWEEP_ANGLE) was issued.
//     Servo is physically moving toward SWEEP_ANGLE (the servo slews
//     at its maximum rate — we don't need to drive it incrementally).
//     After SWEEP_DURATION_MS, transition to RETURNING.
//
//   RETURNING:
//     write_servo_home() was issued.
//     Servo is physically returning to angle_home.
//     After RETURN_DURATION_MS, transition to IDLE.
//     LED is turned off when BOTH servos are IDLE.
//
// Overflow-safe timing: (millis() - start_ms) >= duration

void check_servo_state() {
  uint32_t now = millis();

  // ── Servo 1 ─────────────────────────────────────────────────────
  if (servo1_phase == PHASE_SWEEPING) {
    if ((now - servo1_phase_start_ms) >= servo1_sweep_duration_ms) {
      // Sweep complete → command return to home
      write_servo_home(1);
      servo1_phase          = PHASE_RETURNING;
      servo1_phase_start_ms = now;  // start return timer
    }
  }
  else if (servo1_phase == PHASE_RETURNING) {
    if ((now - servo1_phase_start_ms) >= servo1_return_duration_ms) {
      servo1_phase = PHASE_IDLE;
      if (servo2_phase == PHASE_IDLE) {
        digitalWrite(PIN_STATUS_LED, LOW);  // both idle → LED off
      }
    }
  }

  // ── Servo 2 ─────────────────────────────────────────────────────
  if (servo2_phase == PHASE_SWEEPING) {
    if ((now - servo2_phase_start_ms) >= servo2_sweep_duration_ms) {
      write_servo_home(2);
      servo2_phase          = PHASE_RETURNING;
      servo2_phase_start_ms = now;
    }
  }
  else if (servo2_phase == PHASE_RETURNING) {
    if ((now - servo2_phase_start_ms) >= servo2_return_duration_ms) {
      servo2_phase = PHASE_IDLE;
      if (servo1_phase == PHASE_IDLE) {
        digitalWrite(PIN_STATUS_LED, LOW);
      }
    }
  }
}

// ── Main loop ─────────────────────────────────────────────────────────────
void loop() {
  // ── 1. Service servo state machine ────────────────────────────────
  check_servo_state();

  // ── 2. Process IR1 ────────────────────────────────────────────────
  if (ir1_pending) {
    ir1_pending = false;
    uint32_t now = millis();
    if (now - last_ir1_ms >= DEBOUNCE_MS) {
      last_ir1_ms = now;
      send_ir_trigger(1, now);
    }
  }

  // ── 3. Process IR2 ────────────────────────────────────────────────
  if (ir2_pending) {
    ir2_pending = false;
    uint32_t now = millis();
    if (now - last_ir2_ms >= DEBOUNCE_MS) {
      last_ir2_ms = now;
      send_ir_trigger(2, now);
    }
  }

  // ── 4. Process incoming Serial commands ───────────────────────────
  while (Serial.available() > 0) {
    char c = Serial.read();
    if (c == '\n' || c == '\r') {
      if (serial_buf_index > 0) {
        serial_buffer[serial_buf_index] = '\0';
        handle_command(serial_buffer);
        serial_buf_index = 0;
      }
    }
    else if (serial_buf_index < sizeof(serial_buffer) - 1) {
      serial_buffer[serial_buf_index++] = c;
    }
    else {
      send_error("cmd_too_long");
      serial_buf_index = 0;
      while (Serial.available() > 0 && Serial.read() != '\n') { ; }
    }
  }
}

// ── Send IR_TRIGGER event to Master ───────────────────────────────────────
void send_ir_trigger(uint8_t sensor_id, uint32_t ts) {
  StaticJsonDocument<96> doc;
  doc["ack"]    = "IR_TRIGGER";
  doc["sensor"] = sensor_id;
  doc["ts"]     = ts;
  serializeJson(doc, Serial);
  Serial.println();
}

// ── Parse and dispatch a command from Master ──────────────────────────────
void handle_command(const char* raw) {
  StaticJsonDocument<256> doc;
  DeserializationError err = deserializeJson(doc, raw);
  if (err) { send_error("json_parse_fail"); return; }

  const char* cmd = doc["cmd"] | "";

  if (strcmp(cmd, "SORT") == 0) {
    uint8_t     servo_id   = doc["servo"]     | 0;
    if (servo_id != 1 && servo_id != 2) {
      send_error("bad_servo");
      return;
    }

    const char* direction  = doc["dir"]       | "home";
    int         angle      = doc["angle"]     | 120;      // read angle from JSON
    int         home_angle = (servo_id == 1)
                             ? (doc["home"] | servo1_home_angle)
                             : (doc["home"] | servo2_home_angle);
    int         max_angle  = (servo_id == 1)
                             ? (doc["max"] | servo1_max_angle)
                             : (doc["max"] | servo2_max_angle);
    int         pulse_min  = (servo_id == 1)
                             ? (doc["min_us"] | servo1_pulse_min_us)
                             : (doc["min_us"] | servo2_pulse_min_us);
    int         pulse_max  = (servo_id == 1)
                             ? (doc["max_us"] | servo1_pulse_max_us)
                             : (doc["max_us"] | servo2_pulse_max_us);
    int         sweep_ms   = doc["sweep_ms"]  | DEFAULT_SWEEP_DURATION_MS;
    int         return_ms  = doc["return_ms"] | DEFAULT_RETURN_DURATION_MS;

    actuate_servo(
      servo_id, direction, angle, home_angle, max_angle,
      pulse_min, pulse_max, sweep_ms, return_ms
    );

    // ACK immediately — actual sweep runs asynchronously in loop().
    // total_ms = sweep_ms + return_ms (nominal time for caller info)
    uint16_t total = (servo_id == 1) 
                     ? servo1_sweep_duration_ms + servo1_return_duration_ms
                     : servo2_sweep_duration_ms + servo2_return_duration_ms;
    StaticJsonDocument<128> resp;
    resp["ack"]      = "SORT_DONE";
    resp["servo"]    = servo_id;
    resp["angle"]    = angle;
    resp["home"]     = home_angle;
    resp["total_ms"] = total;
    serializeJson(resp, Serial);
    Serial.println();
  }

  else if (strcmp(cmd, "PING") == 0) {
    StaticJsonDocument<80> resp;
    resp["ack"]      = "PONG";
    resp["uptime_s"] = (millis() - boot_ms) / 1000UL;
    serializeJson(resp, Serial);
    Serial.println();
  }

  else if (strcmp(cmd, "RESET") == 0) {
    int max_angle = doc["max"] | DEFAULT_SERVO_MAX_ANGLE;
    int pulse_min = doc["min_us"] | DEFAULT_PULSE_MIN_US;
    int pulse_max = doc["max_us"] | DEFAULT_PULSE_MAX_US;

    store_servo_config(
      1, doc["home1"] | servo1_home_angle,
      doc["max1"] | max_angle,
      doc["min1_us"] | pulse_min,
      doc["max1_us"] | pulse_max
    );
    store_servo_config(
      2, doc["home2"] | servo2_home_angle,
      doc["max2"] | max_angle,
      doc["min2_us"] | pulse_min,
      doc["max2_us"] | pulse_max
    );

    write_servo_home(1);
    write_servo_home(2);
    servo1_phase = PHASE_IDLE;
    servo2_phase = PHASE_IDLE;
    digitalWrite(PIN_STATUS_LED, LOW);
    Serial.println("{\"ack\":\"RESET_DONE\"}");
  }

  else if (strcmp(cmd, "STATUS") == 0) {
    // Compute remaining time in current phase (overflow-safe)
    uint32_t now = millis();

    int32_t s1_remaining_ms = 0;
    if (servo1_phase == PHASE_SWEEPING) {
      uint32_t elapsed = now - servo1_phase_start_ms;
      s1_remaining_ms  = (int32_t)servo1_sweep_duration_ms - (int32_t)elapsed
                        + (int32_t)servo1_return_duration_ms;
    } else if (servo1_phase == PHASE_RETURNING) {
      uint32_t elapsed = now - servo1_phase_start_ms;
      s1_remaining_ms  = (int32_t)servo1_return_duration_ms - (int32_t)elapsed;
    }

    int32_t s2_remaining_ms = 0;
    if (servo2_phase == PHASE_SWEEPING) {
      uint32_t elapsed = now - servo2_phase_start_ms;
      s2_remaining_ms  = (int32_t)servo2_sweep_duration_ms - (int32_t)elapsed
                        + (int32_t)servo2_return_duration_ms;
    } else if (servo2_phase == PHASE_RETURNING) {
      uint32_t elapsed = now - servo2_phase_start_ms;
      s2_remaining_ms  = (int32_t)servo2_return_duration_ms - (int32_t)elapsed;
    }

    StaticJsonDocument<384> resp;
    resp["ack"]            = "STATUS";
    resp["servo1_ok"]      = servo1.attached();
    resp["servo2_ok"]      = servo2.attached();
    resp["servo1_phase"]   = (int)servo1_phase;   // 0=IDLE,1=SWEEPING,2=RETURNING
    resp["servo2_phase"]   = (int)servo2_phase;
    resp["servo1_home"]    = servo1_home_angle;
    resp["servo2_home"]    = servo2_home_angle;
    resp["servo1_max"]     = servo1_max_angle;
    resp["servo2_max"]     = servo2_max_angle;
    resp["servo1_rem_ms"]  = max(0, s1_remaining_ms);
    resp["servo2_rem_ms"]  = max(0, s2_remaining_ms);
    resp["ir1_pin"]        = digitalRead(PIN_IR1);
    resp["ir2_pin"]        = digitalRead(PIN_IR2);
    resp["uptime_s"]       = (millis() - boot_ms) / 1000UL;
    serializeJson(resp, Serial);
    Serial.println();
  }

  else {
    send_error("unknown_cmd");
  }
}

// ── Actuate a servo — NON-BLOCKING SWEEP ─────────────────────────────────
//
// "fire" direction:
//   Immediately write sweep_angle to the servo (read from JSON command).
//   The servo will physically slew from angle_home to sweep_angle at full speed.
//   check_servo_state() monitors sweep_duration_ms then commands the return.
//   Both sweep_duration_ms and return_duration_ms are now read from JSON,
//   synced with Raspberry Pi config (hardware_config.yaml).
//
// "home" / any other direction:
//   Immediately write this servo's dynamic HOME angle. Useful for RESET commands.
//
// Concurrency note: if a second SORT arrives while servo is already
//   sweeping/returning, we restart the sweep phase. This is safe on
//   single-core AVR because Serial commands are processed sequentially.

void actuate_servo(uint8_t id, const char* direction, int sweep_angle,
                   int home_angle, int max_angle,
                   int pulse_min_us, int pulse_max_us,
                   int sweep_ms, int return_ms) {
  Servo&      srv        = (id == 1) ? servo1          : servo2;
  ServoPhase& phase_ref  = (id == 1) ? servo1_phase    : servo2_phase;
  uint32_t&   start_ref  = (id == 1) ? servo1_phase_start_ms : servo2_phase_start_ms;

  store_servo_config(id, home_angle, max_angle, pulse_min_us, pulse_max_us);

  // Store dynamic timing parameters for this servo
  if (id == 1) {
    servo1_sweep_duration_ms  = sweep_ms;
    servo1_return_duration_ms = return_ms;
  } else {
    servo2_sweep_duration_ms  = sweep_ms;
    servo2_return_duration_ms = return_ms;
  }

  if (strcmp(direction, "fire") == 0) {
    // Command the sweep angle from JSON (synced with Raspberry Pi config).
    // 270° servo angles are mapped to PWM pulses by write_servo_angle().
    if (id == 1) {
      write_servo_angle(
        srv, sweep_angle, servo1_max_angle,
        servo1_pulse_min_us, servo1_pulse_max_us
      );
    } else {
      write_servo_angle(
        srv, sweep_angle, servo2_max_angle,
        servo2_pulse_min_us, servo2_pulse_max_us
      );
    }
    digitalWrite(PIN_STATUS_LED, HIGH);
    phase_ref = PHASE_SWEEPING;
    start_ref = millis();
  } else {
    // "home" or any other value → return to rest immediately
    write_servo_home(id);
    phase_ref = PHASE_IDLE;
    if ((id == 1 && servo2_phase == PHASE_IDLE) ||
        (id == 2 && servo1_phase == PHASE_IDLE)) {
      digitalWrite(PIN_STATUS_LED, LOW);
    }
  }
}

// ── Send error message to Master ──────────────────────────────────────────
void send_error(const char* msg) {
  StaticJsonDocument<64> doc;
  doc["ack"] = "ERROR";
  doc["msg"] = msg;
  serializeJson(doc, Serial);
  Serial.println();
}
