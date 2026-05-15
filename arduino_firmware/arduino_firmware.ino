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
 * PROBLEM: The previous firmware called millis() inside the ISR to
 * implement debouncing.  millis() internally reads the timer0 overflow
 * counter, which is updated by its own interrupt.  Calling millis()
 * inside another ISR can produce incorrect values because timer0
 * interrupts are masked while the ISR runs on AVR (no nested interrupts
 * by default), and the value may be stale or partially updated.
 *
 * RULE: ISRs must be as short as possible.  They should only:
 *   • Set a volatile flag (done atomically on AVR).
 *   • Optionally record a pre-computed timestamp.
 * All other logic must live in loop().
 *
 * FIXED PATTERN:
 *   ISR  → sets volatile bool ir1_pending = true.
 *            Records nothing (no millis() call here).
 *   loop → when ir1_pending is seen:
 *            1. Read millis() safely (interrupts re-enabled in loop).
 *            2. Compare with last_ir1_ms for debounce.
 *            3. If debounce passes: send IR_TRIGGER, update last_ir1_ms.
 *            4. Clear ir1_pending.
 *
 * This guarantees:
 *   • ISR executes in < 10 CPU cycles (set one byte, reti).
 *   • millis() is only ever called from loop() where interrupts are on.
 *   • Debounce logic is deterministic and fully testable on a PC.
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
//
// volatile: prevents the compiler from caching in a register.
// The ISR writes; loop() reads and clears.  On AVR, a single-byte
// read/write is atomic, so no additional locking is needed here.
volatile bool ir1_pending = false;
volatile bool ir2_pending = false;

// ── Debounce state — owned exclusively by loop() ──────────────────────────
//
// These variables are read and written only inside loop(), which runs with
// interrupts enabled.  They are NOT volatile because ISRs never touch them.
uint32_t last_ir1_ms = 0;
uint32_t last_ir2_ms = 0;

// ── Other state ───────────────────────────────────────────────────────────
Servo    servo1, servo2;
bool     servo1_busy = false;
bool     servo2_busy = false;
uint32_t boot_ms     = 0;

// ── ISRs ──────────────────────────────────────────────────────────────────
//
// Each ISR does exactly ONE thing: set a flag.
// No millis(), no Serial, no arithmetic — just a single volatile write.
// This keeps ISR latency under ~6 CPU cycles on an ATmega328P.

void isr_ir1() {
  ir1_pending = true;
}

void isr_ir2() {
  ir2_pending = true;
}

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
  doc["firmware"] = "FruitSorter-v2.1";
  serializeJson(doc, Serial);
  Serial.println();

  // Startup blink
  for (int i = 0; i < 3; i++) {
    digitalWrite(PIN_STATUS_LED, HIGH); delay(100);
    digitalWrite(PIN_STATUS_LED, LOW);  delay(100);
  }
}

// ── Main loop ─────────────────────────────────────────────────────────────
//
// Interrupts are ENABLED here, so millis() is safe to call.
// Debounce and event emission both live here — not in the ISRs.

void loop() {
  // ── Process IR1 ──────────────────────────────────────────────────────
  //
  // Read the volatile flag into a local variable, clear it atomically,
  // then perform debounce.  The flag is cleared before the debounce check
  // so that rapid re-triggers are not silently lost: if ir1_pending is
  // set again while we process this one, we will see it on the next
  // loop() iteration.
  if (ir1_pending) {
    ir1_pending = false;                      // clear BEFORE reading millis()
    uint32_t now = millis();                  // safe here — interrupts ON
    if (now - last_ir1_ms >= DEBOUNCE_MS) {
      last_ir1_ms = now;
      send_ir_trigger(1, now);
    }
  }

  // ── Process IR2 ──────────────────────────────────────────────────────
  if (ir2_pending) {
    ir2_pending = false;
    uint32_t now = millis();
    if (now - last_ir2_ms >= DEBOUNCE_MS) {
      last_ir2_ms = now;
      send_ir_trigger(2, now);
    }
  }

  // ── Process incoming Serial commands ─────────────────────────────────
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

    uint32_t t0 = millis();
    actuate_servo(servo_id, direction);
    uint32_t dur = millis() - t0;

    StaticJsonDocument<80> resp;
    resp["ack"]   = "SORT_DONE";
    resp["servo"] = servo_id;
    resp["ms"]    = dur;
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
    servo1_busy = false;
    servo2_busy = false;
    Serial.println("{\"ack\":\"RESET_DONE\"}");
  }

  else if (strcmp(cmd, "STATUS") == 0) {
    StaticJsonDocument<128> resp;
    resp["ack"]         = "STATUS";
    resp["servo1_ok"]   = servo1.attached();
    resp["servo2_ok"]   = servo2.attached();
    resp["servo1_busy"] = servo1_busy;
    resp["servo2_busy"] = servo2_busy;
    resp["ir1_pin"]     = digitalRead(PIN_IR1);
    resp["ir2_pin"]     = digitalRead(PIN_IR2);
    resp["uptime_s"]    = (millis() - boot_ms) / 1000UL;
    serializeJson(resp, Serial);
    Serial.println();
  }

  else {
    send_error("unknown_cmd");
  }
}

// ── Actuate a servo by id and direction ──────────────────────────────────
void actuate_servo(uint8_t id, const char* direction) {
  Servo& srv      = (id == 1) ? servo1 : servo2;
  bool&  busy_ref = (id == 1) ? servo1_busy : servo2_busy;

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

  busy_ref = true;
  digitalWrite(PIN_STATUS_LED, HIGH);
  srv.write(angle);
  delay(SERVO_HOLD_MS);
  srv.write((id == 1) ? S1_NEUTRAL : S2_NEUTRAL);
  digitalWrite(PIN_STATUS_LED, LOW);
  busy_ref = false;
}

// ── Send an error message to Master ──────────────────────────────────────
void send_error(const char* msg) {
  StaticJsonDocument<64> doc;
  doc["ack"] = "ERROR";
  doc["msg"] = msg;
  serializeJson(doc, Serial);
  Serial.println();
}
