/*
 * arduino_firmware/arduino_firmware.ino
 * =====================================================================
 * FruitSorter — Arduino Slave Firmware
 * =====================================================================
 * Vai trò: Slave nhận lệnh từ Raspberry Pi (Master) qua UART Serial
 *
 * Nhiệm vụ:
 *   1. Đọc 2× IR sensor bằng hardware interrupt (pin 2, 3)
 *   2. Gửi IR_TRIGGER event lên Master ngay khi phát hiện
 *   3. Nhận lệnh SORT từ Master → kích Servo tương ứng
 *   4. Trả lời PING bằng PONG (heartbeat watchdog)
 *   5. Báo STATUS theo yêu cầu
 *
 * Giao thức: JSON one-liner + '\n' @ 115200 baud
 *
 * Thư viện cần cài (Arduino IDE → Library Manager):
 *   - ArduinoJson  (by Benoit Blanchon)  ← bắt buộc
 *   - Servo        (built-in)
 *
 * Pin layout (khớp với hardware_config.yaml):
 *   IR Sensor 1 → Digital Pin 2  (INT0, FALLING)
 *   IR Sensor 2 → Digital Pin 3  (INT1, FALLING)
 *   Servo 1     → PWM Pin 9
 *   Servo 2     → PWM Pin 10
 *   Status LED  → Pin 13 (built-in)
 * =====================================================================
 */

#include <Servo.h>
#include <ArduinoJson.h>

// ── Pin definitions (sync với hardware_config.yaml) ─────────────────
#define PIN_IR1         2
#define PIN_IR2         3
#define PIN_SERVO1      9
#define PIN_SERVO2      10
#define PIN_STATUS_LED  13

// ── Servo angles (sync với hardware_config.yaml) ─────────────────────
#define S1_NEUTRAL    90
#define S1_LEFT       45
#define S1_RIGHT     135
#define S2_NEUTRAL    90
#define S2_LEFT       50
#define S2_RIGHT     130
#define SERVO_HOLD_MS 500

// ── Timing ───────────────────────────────────────────────────────────
#define DEBOUNCE_MS     20
#define SERIAL_BAUD  115200
#define LED_BLINK_MS   100

// ── State ────────────────────────────────────────────────────────────
Servo servo1, servo2;

volatile bool     ir1_flag = false;
volatile bool     ir2_flag = false;
volatile uint32_t ir1_ts   = 0;
volatile uint32_t ir2_ts   = 0;
uint32_t          last_ir1 = 0;
uint32_t          last_ir2 = 0;
uint32_t          boot_ms  = 0;

bool servo1_busy = false;
bool servo2_busy = false;

// ── ISR — hardware interrupt, cực nhanh ─────────────────────────────
void isr_ir1() {
  uint32_t now = millis();
  if (now - last_ir1 >= DEBOUNCE_MS) {
    ir1_flag = true;
    ir1_ts   = now;
    last_ir1 = now;
  }
}

void isr_ir2() {
  uint32_t now = millis();
  if (now - last_ir2 >= DEBOUNCE_MS) {
    ir2_flag = true;
    ir2_ts   = now;
    last_ir2 = now;
  }
}

// ── Setup ─────────────────────────────────────────────────────────────
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
  doc["firmware"] = "FruitSorter-v2.0";
  serializeJson(doc, Serial);
  Serial.println();

  for (int i = 0; i < 3; i++) {
    digitalWrite(PIN_STATUS_LED, HIGH); delay(100);
    digitalWrite(PIN_STATUS_LED, LOW);  delay(100);
  }
}

// ── Main loop ──────────────────────────────────────────────────────────
void loop() {
  if (ir1_flag) {
    ir1_flag = false;
    send_ir_trigger(1, ir1_ts);
  }
  if (ir2_flag) {
    ir2_flag = false;
    send_ir_trigger(2, ir2_ts);
  }

  if (Serial.available() > 0) {
    String line = Serial.readStringUntil('\n');
    line.trim();
    if (line.length() > 0) {
      handle_command(line);
    }
  }
}

// ── Gửi IR_TRIGGER event lên Master ─────────────────────────────────
void send_ir_trigger(uint8_t sensor_id, uint32_t ts) {
  StaticJsonDocument<96> doc;
  doc["ack"]    = "IR_TRIGGER";
  doc["sensor"] = sensor_id;
  doc["ts"]     = ts;
  serializeJson(doc, Serial);
  Serial.println();
}

// ── Nhận và parse lệnh từ Master ─────────────────────────────────────
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
    resp["ack"]          = "STATUS";
    resp["servo1_ok"]    = servo1.attached();
    resp["servo2_ok"]    = servo2.attached();
    resp["servo1_busy"]  = servo1_busy;
    resp["servo2_busy"]  = servo2_busy;
    resp["ir1_pin"]      = digitalRead(PIN_IR1);
    resp["ir2_pin"]      = digitalRead(PIN_IR2);
    resp["uptime_s"]     = (millis() - boot_ms) / 1000UL;
    serializeJson(resp, Serial);
    Serial.println();
  }

  else {
    send_error("unknown_cmd");
  }
}

// ── Kích servo theo id và direction ──────────────────────────────────
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

// ── Gửi error message lên Master ─────────────────────────────────────
void send_error(const char* msg) {
  StaticJsonDocument<64> doc;
  doc["ack"] = "ERROR";
  doc["msg"] = msg;
  serializeJson(doc, Serial);
  Serial.println();
}