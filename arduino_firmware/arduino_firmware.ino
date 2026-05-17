/*
 * arduino_firmware/arduino_firmware.ino
 * =====================================================================
 * FruitSorter — Arduino Slave Firmware
 * =====================================================================
 * Role   : Slave — receives commands from the Raspberry Pi (Master)
 *          via UART Serial.
 *
 * Tasks:
 *   1. Read 2× IR sensors using hardware interrupts (pins 2, 3)
 *   2. Send IR_TRIGGER events to Master as soon as a sensor fires
 *   3. Receive SORT commands from Master → actuate the correct servo
 *   4. Reply PONG to PING (heartbeat watchdog)
 *   5. Report STATUS on demand
 *
 * Protocol: JSON one-liner + '\n' @ 115200 baud
 *
 * Libraries required (Arduino IDE → Library Manager):
 *   - ArduinoJson  (by Benoit Blanchon)  ← mandatory
 *   - Servo        (built-in)
 *
 * Pin layout (matches hardware_config.yaml):
 *   IR Sensor 1 → Digital Pin 2  (INT0, FALLING)
 *   IR Sensor 2 → Digital Pin 3  (INT1, FALLING)
 *   Servo 1     → PWM Pin 9
 *   Servo 2     → PWM Pin 10
 *   Status LED  → Pin 13 (built-in)
 *
 * =====================================================================
 * ISR-safety fix (Issue #2)
 * =====================================================================
 * ISRs only set a volatile flag — no millis(), no Serial.
 * Debounce and event emission both live in loop().
 * See original comments for full rationale.
 *
 * =====================================================================
 * Bug fix — Non-blocking servo actuation
 * =====================================================================
 * PROBLEM: The previous firmware called delay(SERVO_HOLD_MS) inside
 * actuate_servo(), which is invoked from loop():
 *
 *   void actuate_servo(...) {
 *       srv.write(angle);
 *       delay(SERVO_HOLD_MS);   // ← busy-wait, blocks ALL of loop()
 *       srv.write(neutral);
 *   }
 *
 * delay() is a busy-wait on AVR — it spins until the timer counter
 * reaches the target value.  While spinning, loop() cannot run.
 * Hardware interrupts still fire (ISRs execute), so ir1_pending /
 * ir2_pending get set correctly — but because loop() is blocked,
 * those flags are never read, debounced, or forwarded to the Master
 * until delay() returns.
 *
 * Real-world impact: conveyor at 0.3 m/s, fruit spacing 15 cm →
 * inter-fruit interval ≈ 500 ms = SERVO_HOLD_MS.  Any IR event that
 * fires while the servo is holding its angle is silently dropped.
 * The Master never sees the trigger, so the second fruit is never
 * sorted.
 *
 * FIXED PATTERN — software timer (non-blocking):
 *
 *   actuate_servo() now:
 *     1. Writes the target angle to the servo.
 *     2. Records the scheduled return time:
 *          servo_return_at = millis() + SERVO_HOLD_MS
 *     3. Sets a servo_returning flag.
 *     4. Returns immediately — does NOT block.
 *
 *   check_servo_returns(), called at the TOP of every loop() iteration:
 *     1. Reads millis().
 *     2. For each servo: if returning && now >= return_at → write neutral.
 *     3. Clears the returning flag and busy flag.
 *
 * This keeps loop() running continuously regardless of servo state.
 * IR events are processed within one loop() iteration (< 1 ms) even
 * while a servo is holding its sort position.
 *
 * SORT_DONE response change:
 *   Because actuate_servo() now returns before the hold completes, we
 *   cannot measure the actual hold duration with millis() anymore.
 *   The SORT_DONE ACK is sent immediately with the *nominal* hold time
 *   (SERVO_HOLD_MS constant) instead of a measured value.  The Master
 *   already treats SORT_DONE as "command accepted", not "servo returned
 *   to neutral", so this semantic change is compatible.
 * =====================================================================
 */

#include <Servo.h>
#include <ArduinoJson.h>

// ── Pin definitions (sync with hardware_config.yaml) ─────────────────────
#define PIN_IR1         2
#define PIN_IR2         3
#define PIN_SERVO1      9
#define PIN_SERVO2      10
#define PIN_STATUS_LED  13

// ── Servo angles (sync with hardware_config.yaml) ─────────────────────────
#define S1_NEUTRAL    90
#define S1_LEFT       45
#define S1_RIGHT     135
#define S2_NEUTRAL    90
#define S2_LEFT       50
#define S2_RIGHT     130
#define SERVO_HOLD_MS 500

// ── Timing ────────────────────────────────────────────────────────────────
#define DEBOUNCE_MS     20   // minimum ms between two valid triggers
#define SERIAL_BAUD  115200

// ── ISR state — ONLY flags; no timestamps, no millis() calls ─────────────
volatile bool ir1_pending = false;
volatile bool ir2_pending = false;

// ── Debounce state — owned exclusively by loop() ──────────────────────────
uint32_t last_ir1_ms = 0;
uint32_t last_ir2_ms = 0;

// ── Servo state ───────────────────────────────────────────────────────────
Servo    servo1, servo2;

// busy flag: true while the servo is away from neutral (sorting or returning)
bool     servo1_busy = false;
bool     servo2_busy = false;

// Non-blocking return timer state.
// returning flag : true while the servo is holding its sort angle and
//                  waiting to return to neutral.
// return_at      : the millis() value at which to write neutral.
bool     servo1_returning = false;
bool     servo2_returning = false;
uint32_t servo1_return_at = 0;
uint32_t servo2_return_at = 0;

// ── Other state ───────────────────────────────────────────────────────────
uint32_t boot_ms = 0;

// ── ISRs ──────────────────────────────────────────────────────────────────
void isr_ir1() { ir1_pending = true; }
void isr_ir2() { ir2_pending = true; }

// ── Setup ─────────────────────────────────────────────────────────────────
void setup() {
  Serial.begin(SERIAL_BAUD);
  while (!Serial) { ; }

  servo1.attach(PIN_SERVO1);
  servo2.attach(PIN_SERVO2);
  servo1.write(S1_NEUTRAL);
  servo2.write(S2_NEUTRAL);

  pinMode(PIN_IR1, INPUT_PULLUP);
  pinMode(PIN_IR2, INPUT_PULLUP);
  attachInterrupt(digitalPinToInterrupt(PIN_IR1), isr_ir1, FALLING);
  attachInterrupt(digitalPinToInterrupt(PIN_IR2), isr_ir2, FALLING);

  pinMode(PIN_STATUS_LED, OUTPUT);
  digitalWrite(PIN_STATUS_LED, LOW);

  boot_ms = millis();

  StaticJsonDocument<64> doc;
  doc["boot"]     = "ok";
  doc["firmware"] = "FruitSorter-v2.2";
  serializeJson(doc, Serial);
  Serial.println();

  // Startup blink
  for (int i = 0; i < 3; i++) {
    digitalWrite(PIN_STATUS_LED, HIGH); delay(100);
    digitalWrite(PIN_STATUS_LED, LOW);  delay(100);
  }
}

// ── Non-blocking servo return checker ─────────────────────────────────────
//
// Called at the TOP of every loop() iteration — O(1), always fast.
// Checks whether either servo has held its sort angle long enough and
// needs to return to neutral.
//
// This replaces the blocking delay(SERVO_HOLD_MS) that was inside
// actuate_servo().  Because this runs on every loop() pass, the servo
// returns to neutral within 1 loop iteration (< 1 ms) of the deadline,
// which is far more accurate than a busy-wait delay() and — crucially —
// does not block IR event processing.

void check_servo_returns() {
  uint32_t now = millis();

  if (servo1_returning && (now >= servo1_return_at)) {
    servo1.write(S1_NEUTRAL);
    servo1_busy      = false;
    servo1_returning = false;
    // Turn off the LED only when both servos are back at neutral
    if (!servo2_busy) {
      digitalWrite(PIN_STATUS_LED, LOW);
    }
  }

  if (servo2_returning && (now >= servo2_return_at)) {
    servo2.write(S2_NEUTRAL);
    servo2_busy      = false;
    servo2_returning = false;
    if (!servo1_busy) {
      digitalWrite(PIN_STATUS_LED, LOW);
    }
  }
}

// ── Main loop ─────────────────────────────────────────────────────────────
//
// Interrupts are ENABLED here, so millis() is safe to call.
// check_servo_returns() runs first every iteration so return timing is
// as accurate as possible regardless of Serial or IR processing time.

void loop() {
  // ── 1. Service servo return timers (non-blocking) ─────────────────
  check_servo_returns();

  // ── 2. Process IR1 ───────────────────────────────────────────────
  if (ir1_pending) {
    ir1_pending = false;                      // clear BEFORE reading millis()
    uint32_t now = millis();
    if (now - last_ir1_ms >= DEBOUNCE_MS) {
      last_ir1_ms = now;
      send_ir_trigger(1, now);
    }
  }

  // ── 3. Process IR2 ───────────────────────────────────────────────
  if (ir2_pending) {
    ir2_pending = false;
    uint32_t now = millis();
    if (now - last_ir2_ms >= DEBOUNCE_MS) {
      last_ir2_ms = now;
      send_ir_trigger(2, now);
    }
  }

  // ── 4. Process incoming Serial commands ──────────────────────────
  if (Serial.available() > 0) {
    String line = Serial.readStringUntil('\n');
    line.trim();
    if (line.length() > 0) {
      handle_command(line);
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

// ── Parse and dispatch a command received from Master ─────────────────────
void handle_command(const String& raw) {
  StaticJsonDocument<128> doc;
  DeserializationError err = deserializeJson(doc, raw);

  if (err) {
    send_error("json_parse_fail");
    return;
  }

  const char* cmd = doc["cmd"] | "";

  if (strcmp(cmd, "SORT") == 0) {
    uint8_t     servo_id  = doc["servo"]  | 0;
    const char* direction = doc["dir"]    | "neutral";

    // actuate_servo() now returns immediately (non-blocking).
    // We report the *nominal* hold time instead of measuring it,
    // because the actual return happens asynchronously in loop().
    actuate_servo(servo_id, direction);

    StaticJsonDocument<80> resp;
    resp["ack"]   = "SORT_DONE";
    resp["servo"] = servo_id;
    resp["ms"]    = SERVO_HOLD_MS;   // nominal — actual return is async
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
    servo1.write(S1_NEUTRAL);
    servo2.write(S2_NEUTRAL);
    servo1_busy      = false;
    servo2_busy      = false;
    servo1_returning = false;
    servo2_returning = false;
    digitalWrite(PIN_STATUS_LED, LOW);
    Serial.println("{\"ack\":\"RESET_DONE\"}");
  }

  else if (strcmp(cmd, "STATUS") == 0) {
    StaticJsonDocument<256> resp;
    resp["ack"]            = "STATUS";
    resp["servo1_ok"]      = servo1.attached();
    resp["servo2_ok"]      = servo2.attached();
    resp["servo1_busy"]    = servo1_busy;
    resp["servo2_busy"]    = servo2_busy;
    resp["servo1_ret_ms"]  = servo1_returning
                               ? (int32_t)(servo1_return_at - millis())
                               : 0;
    resp["servo2_ret_ms"]  = servo2_returning
                               ? (int32_t)(servo2_return_at - millis())
                               : 0;
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

// ── Actuate a servo — NON-BLOCKING ────────────────────────────────────────
//
// Writes the target angle and schedules the return to neutral via a
// software timer.  Returns immediately — does NOT call delay().
//
// The actual return to neutral is performed by check_servo_returns()
// on the next loop() iteration after millis() >= servo_return_at.
//
// Concurrency note: if a second SORT command arrives for the same servo
// while it is still holding its sort angle (servo_busy == true), the
// new command overwrites the angle and resets the return timer.  This
// is safe because all state writes happen in loop() with interrupts
// enabled but Serial reads are sequential — two SORT commands cannot
// be processed simultaneously on a single-core AVR.

void actuate_servo(uint8_t id, const char* direction) {
  Servo&    srv        = (id == 1) ? servo1    : servo2;
  bool&     busy_ref   = (id == 1) ? servo1_busy      : servo2_busy;
  bool&     ret_ref    = (id == 1) ? servo1_returning  : servo2_returning;
  uint32_t& ret_at_ref = (id == 1) ? servo1_return_at  : servo2_return_at;

  int angle;
  if (id == 1) {
    if      (strcmp(direction, "left")  == 0) angle = S1_LEFT;
    else if (strcmp(direction, "right") == 0) angle = S1_RIGHT;
    else                                       angle = S1_NEUTRAL;
  } else {
    if      (strcmp(direction, "left")  == 0) angle = S2_LEFT;
    else if (strcmp(direction, "right") == 0) angle = S2_RIGHT;
    else                                       angle = S2_NEUTRAL;
  }

  // Write target angle
  srv.write(angle);
  digitalWrite(PIN_STATUS_LED, HIGH);

  // Schedule return to neutral — non-blocking
  busy_ref   = true;
  ret_ref    = true;
  ret_at_ref = millis() + SERVO_HOLD_MS;

  // Return immediately.  check_servo_returns() will write neutral
  // once SERVO_HOLD_MS ms have elapsed.
}

// ── Send an error message to Master ───────────────────────────────────────
void send_error(const char* msg) {
  StaticJsonDocument<64> doc;
  doc["ack"] = "ERROR";
  doc["msg"] = msg;
  serializeJson(doc, Serial);
  Serial.println();
}
