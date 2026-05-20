#include <Arduino.h>
#include <ArduinoJson.h>
#include <HTTPClient.h>
#include <WiFi.h>
#include <Wire.h>
#include <mbedtls/md.h>

#include <Adafruit_LIS3DH.h>
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
LaundryDetector detector;
uint32_t cycle_counter = 0;
unsigned long last_motion_ms = 0;
CycleLabel last_posted_label = CycleLabel::Unknown;

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
  lis.getEvent(&previous);

  const unsigned long start = millis();
  float sum_sq = 0.0f;
  float peak = 0.0f;
  uint16_t count = 0;

  while (millis() - start < kSampleWindowMs) {
    sensors_event_t current;
    lis.getEvent(&current);
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
} // namespace

#if LAUNDRY_BUTTON_TEST

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
  Wire.begin(kSdaPin, kSclPin);

  if (!lis.begin(0x18) && !lis.begin(0x19)) {
    Serial.println("sensor_detected=false");
    while (true) {
      delay(1000);
    }
  }

  lis.setRange(LIS3DH_RANGE_2_G);
  lis.setDataRate(LIS3DH_DATARATE_25_HZ);
  WiFi.mode(WIFI_OFF);
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
