#include <Arduino.h>
#include <ArduinoJson.h>
#include <HTTPClient.h>
#include <WiFi.h>
#include <Wire.h>
#include <mbedtls/md.h>

#include <Adafruit_LIS3DH.h>
#include <Adafruit_LSM6DS3.h>
#include <Adafruit_LSM6DS3TRC.h>
#include <Adafruit_LSM6DSOX.h>
#include <Adafruit_Sensor.h>

#include "laundry_detector.h"

#if __has_include("laundry_config.h")
#include "laundry_config.h"
#else
#define WIFI_SSID "configure-me"
#define WIFI_PASSWORD "configure-me"
#define DEVICE_ID "laundry-stack-1"
#define FIRMWARE_VERSION "0.1.0"
#define RELAY_URL "http://192.168.1.50:8088/api/v1/events"
#define DEVICE_SECRET "configure-me"
#endif

namespace {
constexpr uint8_t kLedPin = 2;
constexpr uint8_t kSdaPin = 21;
constexpr uint8_t kSclPin = 22;
constexpr unsigned long kSampleWindowMs = 4000;
constexpr unsigned long kIdlePollMs = 60000;
constexpr unsigned long kRunningPollMs = 20000;

Adafruit_LIS3DH lis = Adafruit_LIS3DH();
Adafruit_LSM6DS3 lsm6ds3 = Adafruit_LSM6DS3();
Adafruit_LSM6DS3TRC lsm6ds3trc = Adafruit_LSM6DS3TRC();
Adafruit_LSM6DSOX lsm6dsox = Adafruit_LSM6DSOX();
LaundryDetector detector;
uint32_t cycle_counter = 0;
unsigned long last_motion_ms = 0;
CycleLabel last_posted_label = CycleLabel::Unknown;

enum class MotionSensor {
  None,
  Lis3dh,
  Lsm6ds3,
  Lsm6ds3trc,
  Lsm6dsox,
};
MotionSensor motion_sensor = MotionSensor::None;

const char *label_to_string(CycleLabel label) {
  switch (label) {
  case CycleLabel::Washer:
    return "washer";
  case CycleLabel::Dryer:
    return "dryer";
  case CycleLabel::Stack:
    return "stack";
  case CycleLabel::Unknown:
  default:
    return "unknown";
  }
}

const char *motion_sensor_to_string(MotionSensor sensor) {
  switch (sensor) {
  case MotionSensor::Lis3dh:
    return "LIS3DH";
  case MotionSensor::Lsm6ds3:
    return "LSM6DS3";
  case MotionSensor::Lsm6ds3trc:
    return "LSM6DS3TRC";
  case MotionSensor::Lsm6dsox:
    return "LSM6DSOX";
  case MotionSensor::None:
  default:
    return "none";
  }
}

bool setup_motion_sensor() {
  Wire.begin(kSdaPin, kSclPin);
  if (lis.begin(0x18) || lis.begin(0x19)) {
    lis.setRange(LIS3DH_RANGE_2_G);
    lis.setDataRate(LIS3DH_DATARATE_25_HZ);
    motion_sensor = MotionSensor::Lis3dh;
    return true;
  }
  if (lsm6ds3.begin_I2C(0x6B) || lsm6ds3.begin_I2C(0x6A)) {
    lsm6ds3.setAccelRange(LSM6DS_ACCEL_RANGE_2_G);
    lsm6ds3.setAccelDataRate(LSM6DS_RATE_26_HZ);
    motion_sensor = MotionSensor::Lsm6ds3;
    return true;
  }
  if (lsm6ds3trc.begin_I2C(0x6B) || lsm6ds3trc.begin_I2C(0x6A)) {
    lsm6ds3trc.setAccelRange(LSM6DS_ACCEL_RANGE_2_G);
    lsm6ds3trc.setAccelDataRate(LSM6DS_RATE_26_HZ);
    motion_sensor = MotionSensor::Lsm6ds3trc;
    return true;
  }
  if (lsm6dsox.begin_I2C(0x6B) || lsm6dsox.begin_I2C(0x6A)) {
    lsm6dsox.setAccelRange(LSM6DS_ACCEL_RANGE_2_G);
    lsm6dsox.setAccelDataRate(LSM6DS_RATE_26_HZ);
    motion_sensor = MotionSensor::Lsm6dsox;
    return true;
  }
  return false;
}

bool read_motion_event(sensors_event_t *event) {
  sensors_event_t gyro_event;
  sensors_event_t temp_event;
  switch (motion_sensor) {
  case MotionSensor::Lis3dh:
    lis.getEvent(event);
    return true;
  case MotionSensor::Lsm6ds3:
    lsm6ds3.getEvent(event, &gyro_event, &temp_event);
    return true;
  case MotionSensor::Lsm6ds3trc:
    lsm6ds3trc.getEvent(event, &gyro_event, &temp_event);
    return true;
  case MotionSensor::Lsm6dsox:
    lsm6dsox.getEvent(event, &gyro_event, &temp_event);
    return true;
  case MotionSensor::None:
  default:
    return false;
  }
}

String hmac_sha256(const String &body) {
  byte digest[32];
  mbedtls_md_context_t ctx;
  const mbedtls_md_info_t *info = mbedtls_md_info_from_type(MBEDTLS_MD_SHA256);
  mbedtls_md_init(&ctx);
  mbedtls_md_setup(&ctx, info, 1);
  mbedtls_md_hmac_starts(&ctx,
                         reinterpret_cast<const unsigned char *>(DEVICE_SECRET),
                         strlen(DEVICE_SECRET));
  mbedtls_md_hmac_update(&ctx,
                         reinterpret_cast<const unsigned char *>(body.c_str()),
                         body.length());
  mbedtls_md_hmac_finish(&ctx, digest);
  mbedtls_md_free(&ctx);

  char hex[65];
  for (int i = 0; i < 32; i++) {
    snprintf(hex + (i * 2), 3, "%02x", digest[i]);
  }
  hex[64] = '\0';
  return String(hex);
}

MotionWindow sample_motion_window() {
  sensors_event_t previous;
  if (!read_motion_event(&previous)) {
    return MotionWindow{millis(), 0, 0.0f, 0.0f};
  }

  const unsigned long start = millis();
  float sum_sq = 0.0f;
  float peak = 0.0f;
  uint16_t count = 0;

  while (millis() - start < kSampleWindowMs) {
    sensors_event_t current;
    if (!read_motion_event(&current)) {
      break;
    }
    const float dx = current.acceleration.x - previous.acceleration.x;
    const float dy = current.acceleration.y - previous.acceleration.y;
    const float dz = current.acceleration.z - previous.acceleration.z;
    const float delta_ms2 = sqrtf((dx * dx) + (dy * dy) + (dz * dz));
    const float delta_mg = (delta_ms2 / 9.80665f) * 1000.0f;

    sum_sq += delta_mg * delta_mg;
    if (delta_mg > peak) {
      peak = delta_mg;
    }
    previous = current;
    count++;
    delay(40);
  }

  const float rms = count == 0 ? 0.0f : sqrtf(sum_sq / count);
  if (rms > 12.0f) {
    last_motion_ms = millis();
  }
  return MotionWindow{millis(), 4, rms, peak};
}

bool connect_wifi() {
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  const unsigned long started = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - started < 15000) {
    delay(250);
  }
  return WiFi.status() == WL_CONNECTED;
}

bool post_done_event(const Decision &decision, const MotionWindow &window) {
  if (!connect_wifi()) {
    Serial.println("event_post wifi_failed=true");
    WiFi.disconnect(true);
    WiFi.mode(WIFI_OFF);
    return false;
  }

  JsonDocument doc;
  const String cycle_id = String("cycle-") + String(cycle_counter);
  const String event_id = cycle_id + String("-done-") + String(window.at_ms);
  doc["device_id"] = DEVICE_ID;
  doc["event_id"] = event_id;
  doc["cycle_id"] = cycle_id;
  doc["state"] = "done_sent";
  doc["cycle_label"] = label_to_string(decision.label);
  doc["motion_rms_mg"] = window.rms_mg;
  doc["last_motion_ms"] = window.at_ms - last_motion_ms;
  doc["firmware_version"] = FIRMWARE_VERSION;

  String body;
  serializeJson(doc, body);

  HTTPClient http;
  http.begin(RELAY_URL);
  http.addHeader("content-type", "application/json");
  http.addHeader("x-laundry-signature", hmac_sha256(body));
  const int status = http.POST(body);
  Serial.printf("event_post status=%d label=%s\n", status, label_to_string(decision.label));
  http.end();
  WiFi.disconnect(true);
  WiFi.mode(WIFI_OFF);
  return status >= 200 && status < 300;
}

bool post_motion_started_event(uint32_t event_counter, float motion_mg) {
  if (!connect_wifi()) {
    Serial.println("motion_post wifi_failed=true");
    WiFi.disconnect(true);
    WiFi.mode(WIFI_OFF);
    return false;
  }

  JsonDocument doc;
  const String event_id = String("motion-") + String(event_counter) + "-" + String(millis());
  doc["device_id"] = DEVICE_ID;
  doc["event_id"] = event_id;
  doc["cycle_id"] = "motion-request-test";
  doc["state"] = "motion_started";
  doc["cycle_label"] = "unknown";
  doc["motion_rms_mg"] = motion_mg;
  doc["last_motion_ms"] = 0;
  doc["firmware_version"] = FIRMWARE_VERSION;

  String body;
  serializeJson(doc, body);

  HTTPClient http;
  http.begin(RELAY_URL);
  http.addHeader("content-type", "application/json");
  http.addHeader("x-laundry-signature", hmac_sha256(body));
  const int status = http.POST(body);
  Serial.printf("motion_post status=%d event_id=%s motion_mg=%.1f\n",
                status,
                event_id.c_str(),
                motion_mg);
  http.end();
  WiFi.disconnect(true);
  WiFi.mode(WIFI_OFF);
  return status >= 200 && status < 300;
}
} // namespace

#if LAUNDRY_I2C_SCAN

void setup() {
  Serial.begin(115200);
  delay(1000);
  pinMode(kLedPin, OUTPUT);
  Wire.begin(kSdaPin, kSclPin);
  WiFi.mode(WIFI_OFF);
  Serial.println("i2c_scan_ready=true sda=21 scl=22");
}

void loop() {
  uint8_t found = 0;
  for (uint8_t address = 1; address < 127; address++) {
    Wire.beginTransmission(address);
    const uint8_t error = Wire.endTransmission();
    if (error == 0) {
      Serial.printf("i2c_found address=0x%02X\n", address);
      for (uint8_t reg : {0x00, 0x0D, 0x0F, 0x75}) {
        Wire.beginTransmission(address);
        Wire.write(reg);
        if (Wire.endTransmission(false) == 0 && Wire.requestFrom(address, static_cast<uint8_t>(1)) == 1) {
          Serial.printf("i2c_reg address=0x%02X reg=0x%02X value=0x%02X\n",
                        address,
                        reg,
                        Wire.read());
        }
      }
      found++;
    }
  }
  Serial.printf("i2c_scan_done found=%u\n", found);
  digitalWrite(kLedPin, found > 0 ? HIGH : LOW);
  delay(2000);
}

#elif LAUNDRY_ACCEL_LED_TEST

namespace {
constexpr uint8_t kPwmChannel = 0;
constexpr uint16_t kPwmFrequency = 5000;
constexpr uint8_t kPwmResolutionBits = 8;

float previous_x = 0.0f;
float previous_y = 0.0f;
float previous_z = 0.0f;
bool have_previous = false;

uint8_t brightness_for_motion(float delta_mg) {
  if (delta_mg < 8.0f) {
    return 4;
  }
  if (delta_mg > 220.0f) {
    return 255;
  }
  return static_cast<uint8_t>(4.0f + ((delta_mg - 8.0f) * (251.0f / 212.0f)));
}
} // namespace

void setup() {
  Serial.begin(115200);
  delay(1000);
  ledcSetup(kPwmChannel, kPwmFrequency, kPwmResolutionBits);
  ledcAttachPin(kLedPin, kPwmChannel);
  ledcWrite(kPwmChannel, 4);
  WiFi.mode(WIFI_OFF);

  if (!setup_motion_sensor()) {
    Serial.println("accel_led_test_ready=false sensor_detected=false");
    while (true) {
      ledcWrite(kPwmChannel, 255);
      delay(100);
      ledcWrite(kPwmChannel, 0);
      delay(100);
    }
  }

  Serial.printf("sensor_type=%s\n", motion_sensor_to_string(motion_sensor));
  Serial.println("accel_led_test_ready=true sensor_detected=true");
}

void loop() {
  sensors_event_t event;
  if (!read_motion_event(&event)) {
    Serial.println("accel_read_error=no_sensor");
    delay(1000);
    return;
  }

  float delta_mg = 0.0f;
  if (have_previous) {
    const float dx = event.acceleration.x - previous_x;
    const float dy = event.acceleration.y - previous_y;
    const float dz = event.acceleration.z - previous_z;
    delta_mg = (sqrtf((dx * dx) + (dy * dy) + (dz * dz)) / 9.80665f) * 1000.0f;
  }

  previous_x = event.acceleration.x;
  previous_y = event.acceleration.y;
  previous_z = event.acceleration.z;
  have_previous = true;

  const uint8_t brightness = brightness_for_motion(delta_mg);
  ledcWrite(kPwmChannel, brightness);
  Serial.printf("accel x=%.3f y=%.3f z=%.3f delta_mg=%.1f led=%u\n",
                event.acceleration.x,
                event.acceleration.y,
                event.acceleration.z,
                delta_mg,
                brightness);
  delay(100);
}

#elif LAUNDRY_MOTION_REQUEST_TEST

namespace {
MovementTrigger motion_request_trigger;
uint32_t motion_request_counter = 0;
float previous_x = 0.0f;
float previous_y = 0.0f;
float previous_z = 0.0f;
bool have_previous = false;

float read_delta_mg() {
  sensors_event_t event;
  if (!read_motion_event(&event)) {
    return 0.0f;
  }

  float delta_mg = 0.0f;
  if (have_previous) {
    const float dx = event.acceleration.x - previous_x;
    const float dy = event.acceleration.y - previous_y;
    const float dz = event.acceleration.z - previous_z;
    delta_mg = (sqrtf((dx * dx) + (dy * dy) + (dz * dz)) / 9.80665f) * 1000.0f;
  }

  previous_x = event.acceleration.x;
  previous_y = event.acceleration.y;
  previous_z = event.acceleration.z;
  have_previous = true;
  return delta_mg;
}
} // namespace

void setup() {
  Serial.begin(115200);
  delay(1000);
  pinMode(kLedPin, OUTPUT);
  digitalWrite(kLedPin, LOW);
  WiFi.mode(WIFI_OFF);

  if (!setup_motion_sensor()) {
    Serial.println("motion_request_test_ready=false sensor_detected=false");
    while (true) {
      digitalWrite(kLedPin, HIGH);
      delay(100);
      digitalWrite(kLedPin, LOW);
      delay(100);
    }
  }

  Serial.printf("sensor_type=%s\n", motion_sensor_to_string(motion_sensor));
  Serial.println("motion_request_test_ready=true threshold_mg=30 confirm_ms=3000 cooldown_ms=60000");
}

void loop() {
  const float delta_mg = read_delta_mg();
  const bool should_post = motion_request_trigger.observe(millis(), delta_mg);
  digitalWrite(kLedPin, delta_mg >= 30.0f ? HIGH : LOW);
  Serial.printf("motion_sample delta_mg=%.1f active=%s should_post=%s\n",
                delta_mg,
                delta_mg >= 30.0f ? "true" : "false",
                should_post ? "true" : "false");

  if (should_post) {
    motion_request_counter++;
    digitalWrite(kLedPin, HIGH);
    const bool ok = post_motion_started_event(motion_request_counter, delta_mg);
    Serial.printf("motion_result ok=%s\n", ok ? "true" : "false");
    digitalWrite(kLedPin, ok ? HIGH : LOW);
    delay(ok ? 1000 : 200);
  }

  delay(100);
}

#elif LAUNDRY_BUTTON_TEST

namespace {
constexpr uint8_t kButtonPin = 0;
constexpr unsigned long kDebounceMs = 300;
unsigned long last_button_ms = 0;
uint32_t button_counter = 0;

bool post_button_event() {
  if (!connect_wifi()) {
    Serial.println("button_post wifi_failed=true");
    WiFi.disconnect(true);
    WiFi.mode(WIFI_OFF);
    return false;
  }

  JsonDocument doc;
  const String event_id = String("button-") + String(button_counter) + "-" + String(millis());
  doc["device_id"] = DEVICE_ID;
  doc["event_id"] = event_id;
  doc["cycle_id"] = "manual-button-test";
  doc["state"] = "button_pressed";
  doc["cycle_label"] = "unknown";
  doc["motion_rms_mg"] = 0.0;
  doc["last_motion_ms"] = 0;
  doc["firmware_version"] = FIRMWARE_VERSION;

  String body;
  serializeJson(doc, body);

  HTTPClient http;
  http.begin(RELAY_URL);
  http.addHeader("content-type", "application/json");
  http.addHeader("x-laundry-signature", hmac_sha256(body));
  const int status = http.POST(body);
  Serial.printf("button_post status=%d event_id=%s\n", status, event_id.c_str());
  http.end();
  WiFi.disconnect(true);
  WiFi.mode(WIFI_OFF);
  return status >= 200 && status < 300;
}
} // namespace

void setup() {
  Serial.begin(115200);
  delay(1000);
  pinMode(kLedPin, OUTPUT);
  pinMode(kButtonPin, INPUT_PULLUP);
  digitalWrite(kLedPin, LOW);
  WiFi.mode(WIFI_OFF);
  Serial.println("button_test_ready=true button_pin=0");
}

void loop() {
  const bool pressed = digitalRead(kButtonPin) == LOW;
  if (pressed && millis() - last_button_ms > kDebounceMs) {
    last_button_ms = millis();
    button_counter++;
    digitalWrite(kLedPin, HIGH);
    const bool ok = post_button_event();
    Serial.printf("button_result ok=%s\n", ok ? "true" : "false");
    delay(ok ? 1000 : 200);
    digitalWrite(kLedPin, LOW);
  }
  delay(20);
}

#else

void setup() {
  Serial.begin(115200);
  delay(1000);
  pinMode(kLedPin, OUTPUT);

  if (!setup_motion_sensor()) {
    Serial.println("sensor_detected=false");
    while (true) {
      delay(1000);
    }
  }

  WiFi.mode(WIFI_OFF);
  Serial.printf("sensor_type=%s\n", motion_sensor_to_string(motion_sensor));
  Serial.println("sensor_detected=true");
}

void loop() {
  MotionWindow window = sample_motion_window();
  Decision decision = detector.observe(window);

  Serial.printf("motion rms_mg=%.2f peak_mg=%.2f state=%d label=%s\n",
                window.rms_mg,
                window.peak_mg,
                static_cast<int>(decision.state),
                label_to_string(decision.label));

  if (decision.should_post) {
    last_posted_label = decision.label;
    post_done_event(decision, window);
  }

  const unsigned long nap_ms =
      decision.state == DetectorState::Idle || decision.state == DetectorState::DoneSent
          ? kIdlePollMs
          : kRunningPollMs;
  delay(nap_ms);
}

#endif
