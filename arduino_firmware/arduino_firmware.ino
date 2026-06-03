/*
 * arduino_firmware/arduino_firmware.ino
 * =====================================================================
 * FruitSorter — Arduino Slave Firmware  v3.0 (SWEEP mechanism)
 * =====================================================================
 * Role   : Slave — receives commands from the Raspberry Pi (Master)
 *          via UART Serial.
 *
 * Tasks:
 *   1. Read 2× IR sensors using hardware interrupts (pins 2, 3)
 *   2. Send IR_TRIGGER events to Master as soon as a sensor fires
 *   3. Receive SORT commands from Master → actuate the correct servo
 *      using SWEEP (0° → 120° fast sweep to deflect fruit sideways)
 *   4. Reply PONG to PING (heartbeat watchdog)
 *   5. Report STATUS on demand
 *
 * Protocol: JSON one-liner + '\n' @ 115200 baud
 *
 * Example commands:
 *   {"cmd":"SORT","servo":1,"dir":"fire","angle":120}
 *   {"cmd":"SORT","servo":1,"dir":"home","angle":0}
 *   {"cmd":"PING"}
 *   {"cmd":"RESET"}
 *   {"cmd":"STATUS"}
 *
 * =====================================================================
 * SWEEP mechanism (v3.0)
 * =====================================================================
 * PREVIOUS design ("push"): servo held a fixed angle and waited for
 * fruit to fall/slide off. Timing-sensitive, position-dependent.
 *
 * NEW design ("sweep" / flap):
 *   1. Flap rests at 0° (parallel to conveyor — no obstruction).
 *   2. On SORT command: servo sweeps 0° → angle (read from JSON, typically 120°)
 *      in SWEEP_DURATION_MS (~200 ms). The EDGE of the flap "slaps"
 *      the fruit sideways as it passes through the station.
 *   3. After sweep completes, servo returns to 0° (home) at a
 *      slightly slower pace (RETURN_DURATION_MS ~300 ms) to avoid
 *      hitting any fruit still on the belt.
 *
 * IMPORTANT: The sweep angle is now READ FROM JSON sent by Raspberry Pi,
 * which reads it from config/hardware_config.yaml. Changing angle_sweep
 * in the YAML file will now take effect immediately — no Arduino recompile needed.
 *
 * Why MG996R works well here:
 *   - Stall torque 9–11 kg·cm @ 6V → ample force for a fast sweep
 *   - No-load speed ~0.14 s/60° @ 6V → 120° sweep in ~280 ms max
 *   - Software-timed sweep is accurate enough (±10 ms on AVR timer)
 *
 * SMOOTH SWEEP via intermediate positions:
 *   Standard Servo.write() gives a step function — instant jump.
 *   For the sweep to "hit" the fruit at maximum angular velocity,
 *   we WANT a fast sweep, so we rely on the servo's own slew rate.
 *   We write the target angle immediately; the servo accelerates
 *   on its own. No intermediate positions needed.
 *
 *   Sequence (non-blocking, managed by check_servo_state()):
 *     Phase IDLE    : servo at HOME (0°)
 *     Phase SWEEPING: Servo.write(SWEEP_ANGLE) issued; wait SWEEP_DURATION_MS
 *     Phase RETURNING: Servo.write(HOME) issued; wait RETURN_DURATION_MS
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

// ── Servo angles ──────────────────────────────────────────────────────────
#define SERVO_HOME          0     // resting position — parallel to belt
// SERVO_SWEEP_ANGLE is now dynamic — read from incoming JSON command

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

// ── Other state ───────────────────────────────────────────────────────────
uint32_t boot_ms = 0;

// ── Serial buffer ─────────────────────────────────────────────────────────
char     serial_buffer[128];
uint8_t  serial_buf_index = 0;

// ── ISRs ──────────────────────────────────────────────────────────────────
void isr_ir1() { ir1_pending = true; }
void isr_ir2() { ir2_pending = true; }

// ── Setup ─────────────────────────────────────────────────────────────────
void setup() {
  Serial.begin(SERIAL_BAUD);
  while (!Serial) { ; }

  servo1.attach(PIN_SERVO1);
  servo2.attach(PIN_SERVO2);
  servo1.write(SERVO_HOME);
  servo2.write(SERVO_HOME);

  pinMode(PIN_IR1, INPUT_PULLUP);
  pinMode(PIN_IR2, INPUT_PULLUP);
  attachInterrupt(digitalPinToInterrupt(PIN_IR1), isr_ir1, FALLING);
  attachInterrupt(digitalPinToInterrupt(PIN_IR2), isr_ir2, FALLING);

  pinMode(PIN_STATUS_LED, OUTPUT);
  digitalWrite(PIN_STATUS_LED, LOW);

  boot_ms = millis();

  StaticJsonDocument<64> doc;
  doc["boot"]     = "ok";
  doc["firmware"] = "FruitSorter-v3.0-sweep";
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
//     servo.write(SWEEP_ANGLE) was issued.
//     Servo is physically moving toward SWEEP_ANGLE (the MG996R slews
//     at its maximum rate — we don't need to drive it incrementally).
//     After SWEEP_DURATION_MS, transition to RETURNING.
//
//   RETURNING:
//     servo.write(HOME) was issued.
//     Servo is physically returning to 0°.
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
      servo1.write(SERVO_HOME);
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
      servo2.write(SERVO_HOME);
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
  StaticJsonDocument<128> doc;
  DeserializationError err = deserializeJson(doc, raw);
  if (err) { send_error("json_parse_fail"); return; }

  const char* cmd = doc["cmd"] | "";

  if (strcmp(cmd, "SORT") == 0) {
    uint8_t     servo_id   = doc["servo"]     | 0;
    const char* direction  = doc["dir"]       | "home";
    int         angle      = doc["angle"]     | 120;      // read angle from JSON
    int         sweep_ms   = doc["sweep_ms"]  | DEFAULT_SWEEP_DURATION_MS;
    int         return_ms  = doc["return_ms"] | DEFAULT_RETURN_DURATION_MS;

    actuate_servo(servo_id, direction, angle, sweep_ms, return_ms);

    // ACK immediately — actual sweep runs asynchronously in loop().
    // total_ms = sweep_ms + return_ms (nominal time for caller info)
    uint16_t total = (servo_id == 1) 
                     ? servo1_sweep_duration_ms + servo1_return_duration_ms
                     : servo2_sweep_duration_ms + servo2_return_duration_ms;
    StaticJsonDocument<96> resp;
    resp["ack"]      = "SORT_DONE";
    resp["servo"]    = servo_id;
    resp["angle"]    = angle;
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
    servo1.write(SERVO_HOME);
    servo2.write(SERVO_HOME);
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

    StaticJsonDocument<256> resp;
    resp["ack"]            = "STATUS";
    resp["servo1_ok"]      = servo1.attached();
    resp["servo2_ok"]      = servo2.attached();
    resp["servo1_phase"]   = (int)servo1_phase;   // 0=IDLE,1=SWEEPING,2=RETURNING
    resp["servo2_phase"]   = (int)servo2_phase;
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
//   The MG996R will physically slew from 0° to sweep_angle at full speed
//   (approx 0.14 s/60° → reaches 120° in ~280 ms).
//   check_servo_state() monitors sweep_duration_ms then commands the return.
//   Both sweep_duration_ms and return_duration_ms are now read from JSON,
//   synced with Raspberry Pi config (hardware_config.yaml).
//
// "home" / any other direction:
//   Immediately write HOME angle. Useful for RESET commands.
//
// Concurrency note: if a second SORT arrives while servo is already
//   sweeping/returning, we restart the sweep phase. This is safe on
//   single-core AVR because Serial commands are processed sequentially.

void actuate_servo(uint8_t id, const char* direction, int sweep_angle, 
                   int sweep_ms, int return_ms) {
  Servo&      srv        = (id == 1) ? servo1          : servo2;
  ServoPhase& phase_ref  = (id == 1) ? servo1_phase    : servo2_phase;
  uint32_t&   start_ref  = (id == 1) ? servo1_phase_start_ms : servo2_phase_start_ms;

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
    // MG996R will reach the target angle under its own speed profile.
    srv.write(sweep_angle);
    digitalWrite(PIN_STATUS_LED, HIGH);
    phase_ref = PHASE_SWEEPING;
    start_ref = millis();
  } else {
    // "home" or any other value → return to rest immediately
    srv.write(SERVO_HOME);
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
