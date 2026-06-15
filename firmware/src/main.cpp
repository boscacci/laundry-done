#include <Arduino.h>
#include <ArduinoJson.h>
#include <HTTPClient.h>
#include <WiFi.h>
#include <Wire.h>
#include <esp_sleep.h>
#include <esp_system.h>
#include <mbedtls/md.h>
#include <time.h>

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

#ifndef WIFI_DEBUG_SSID_PREFIX
#define WIFI_DEBUG_SSID_PREFIX ""
#endif

#ifndef LAUNDRY_KEEP_WIFI_CONNECTED
#define LAUNDRY_KEEP_WIFI_CONNECTED 0
#endif

#ifndef LAUNDRY_USE_LIGHT_SLEEP
#define LAUNDRY_USE_LIGHT_SLEEP 1
#endif

#ifndef LAUNDRY_TRANSMIT_LED_BLINK_MS
#define LAUNDRY_TRANSMIT_LED_BLINK_MS 35
#endif

#ifndef LAUNDRY_KEEPALIVE_WIFI_PULSE
#define LAUNDRY_KEEPALIVE_WIFI_PULSE 1
#endif

namespace {
constexpr uint8_t kLedPin = 2;
constexpr uint8_t kSdaPin = 21;
constexpr uint8_t kSclPin = 22;
constexpr unsigned long kSampleWindowMs = 4000;
constexpr unsigned long kSampleIntervalMs = 40;
constexpr TelemetryCadenceConfig kTelemetryCadence{};

struct WifiTarget {
  bool seen = false;
  int32_t channel = 0;
  int rssi = -127;
  int encryption = 0;
};

Adafruit_LIS3DH lis = Adafruit_LIS3DH();
Adafruit_LSM6DS3 lsm6ds3 = Adafruit_LSM6DS3();
Adafruit_LSM6DS3TRC lsm6ds3trc = Adafruit_LSM6DS3TRC();
Adafruit_LSM6DSOX lsm6dsox = Adafruit_LSM6DSOX();
uint32_t telemetry_counter = 0;
String boot_id;
String telemetry_run_id;
bool clock_configured = false;
bool clock_synced = false;
unsigned long last_active_load_pulse_ms = 0;
LaundryDetector telemetry_cadence_detector(telemetry_cadence_detector_config());

enum class MotionSensor {
  None,
  Lis3dh,
  Lsm6ds3,
  Lsm6ds3trc,
  Lsm6dsox,
};
MotionSensor motion_sensor = MotionSensor::None;

void blink_transmit_led() {
  if (LAUNDRY_TRANSMIT_LED_BLINK_MS == 0) {
    return;
  }
  digitalWrite(kLedPin, HIGH);
  delay(LAUNDRY_TRANSMIT_LED_BLINK_MS);
  digitalWrite(kLedPin, LOW);
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

const char *detector_state_to_string(DetectorState state) {
  switch (state) {
  case DetectorState::Idle:
    return "idle";
  case DetectorState::MotionConfirming:
    return "motion_confirming";
  case DetectorState::CycleRunning:
    return "cycle_running";
  case DetectorState::QuietCandidate:
    return "quiet_candidate";
  case DetectorState::DoneSent:
    return "done_sent";
  default:
    return "unknown";
  }
}

const char *cycle_label_to_string(CycleLabel label) {
  switch (label) {
  case CycleLabel::Unknown:
    return "unknown";
  case CycleLabel::Washer:
    return "washer";
  case CycleLabel::Dryer:
    return "dryer";
  case CycleLabel::Stack:
    return "stack";
  default:
    return "unknown";
  }
}

String make_boot_id() {
  const uint32_t nonce = esp_random();
  return String(millis()) + "-" + String(nonce, HEX);
}

bool clock_is_valid(time_t now) {
  return now > 1704067200; // 2024-01-01T00:00:00Z
}

String format_utc(time_t now) {
  if (!clock_is_valid(now)) {
    return "";
  }
  struct tm utc;
  gmtime_r(&now, &utc);
  char formatted[25];
  strftime(formatted, sizeof(formatted), "%Y-%m-%dT%H:%M:%SZ", &utc);
  return String(formatted);
}

String device_time_utc() {
  return format_utc(time(nullptr));
}

void maybe_sync_clock() {
  if (clock_synced || WiFi.status() != WL_CONNECTED) {
    return;
  }
  if (!clock_configured) {
    configTime(0, 0, "pool.ntp.org", "time.nist.gov");
    clock_configured = true;
  }
  for (uint8_t attempt = 0; attempt < 15; attempt++) {
    const time_t now = time(nullptr);
    if (clock_is_valid(now)) {
      clock_synced = true;
      Serial.printf("time_synced=true epoch=%ld\n", static_cast<long>(now));
      return;
    }
    delay(100);
  }
  Serial.println("time_synced=false");
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
    delay(kSampleIntervalMs);
  }

  const float rms = count == 0 ? 0.0f : sqrtf(sum_sq / count);
  return MotionWindow{millis(), static_cast<uint16_t>((kSampleWindowMs + 999UL) / 1000UL), rms, peak};
}

WifiTarget scan_wifi_target() {
  WifiTarget target;
  const int network_count = WiFi.scanNetworks(false, true);
  uint8_t channel_counts[15] = {0};
  uint32_t target_hash = 2166136261UL;
  for (const char *cursor = WIFI_SSID; *cursor != '\0'; cursor++) {
    target_hash ^= static_cast<uint8_t>(*cursor);
    target_hash *= 16777619UL;
  }
  Serial.printf("wifi_target len=%u hash=%08lx debug_prefix_len=%u\n",
                strlen(WIFI_SSID),
                target_hash,
                strlen(WIFI_DEBUG_SSID_PREFIX));
  for (int i = 0; i < network_count; i++) {
    const String scanned_ssid = WiFi.SSID(i);
    const int32_t channel = WiFi.channel(i);
    uint32_t scanned_hash = 2166136261UL;
    for (uint16_t j = 0; j < scanned_ssid.length(); j++) {
      scanned_hash ^= static_cast<uint8_t>(scanned_ssid[j]);
      scanned_hash *= 16777619UL;
    }
    if (channel >= 1 && channel <= 14) {
      channel_counts[channel]++;
    }
    const bool is_candidate = scanned_ssid == WIFI_SSID;
    const bool is_debug_prefix_match =
        strlen(WIFI_DEBUG_SSID_PREFIX) > 0 &&
        scanned_ssid.startsWith(WIFI_DEBUG_SSID_PREFIX);
    if (is_candidate || is_debug_prefix_match) {
      Serial.printf("wifi_candidate exact=%s len=%u hash=%08lx channel=%d rssi=%d encryption=%d\n",
                    is_candidate ? "true" : "false",
                    scanned_ssid.length(),
                    scanned_hash,
                    channel,
                    WiFi.RSSI(i),
                    WiFi.encryptionType(i));
    }
    if (is_candidate && WiFi.RSSI(i) > target.rssi) {
      target.seen = true;
      target.rssi = WiFi.RSSI(i);
      target.encryption = WiFi.encryptionType(i);
      target.channel = channel;
    }
  }
  Serial.printf("wifi_scan count=%d target_seen=%s target_channel=%d target_rssi=%d target_encryption=%d\n",
                network_count,
                target.seen ? "true" : "false",
                target.channel,
                target.rssi,
                target.encryption);
  Serial.printf("wifi_channel_counts ch1=%u ch2=%u ch3=%u ch4=%u ch5=%u ch6=%u ch7=%u ch8=%u ch9=%u ch10=%u ch11=%u ch12=%u ch13=%u ch14=%u\n",
                channel_counts[1],
                channel_counts[2],
                channel_counts[3],
                channel_counts[4],
                channel_counts[5],
                channel_counts[6],
                channel_counts[7],
                channel_counts[8],
                channel_counts[9],
                channel_counts[10],
                channel_counts[11],
                channel_counts[12],
                channel_counts[13],
                channel_counts[14]);
  WiFi.scanDelete();
  return target;
}

void log_target_wifi_scan() {
  scan_wifi_target();
}

bool battery_keep_awake_active() {
  return millis() < kTelemetryCadence.startup_keep_awake_ms;
}

void maybe_sleep_wifi() {
  if (LAUNDRY_KEEP_WIFI_CONNECTED || battery_keep_awake_active()) {
    return;
  }
  WiFi.disconnect(true);
  WiFi.mode(WIFI_OFF);
}

void power_bank_keepalive_pulse(unsigned long duration_ms, unsigned long remaining_after_ms) {
  Serial.printf("battery_keepalive_pulse duration_ms=%lu remaining_after_ms=%lu mode=%s\n",
                duration_ms,
                remaining_after_ms,
                LAUNDRY_KEEPALIVE_WIFI_PULSE ? "wifi_radio" : "active_delay");
  Serial.flush();
  const unsigned long started_ms = millis();
  digitalWrite(kLedPin, HIGH);
#if LAUNDRY_KEEPALIVE_WIFI_PULSE
  WiFi.persistent(false);
  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);
  if (duration_ms >= 5000UL) {
    const int network_count = WiFi.scanNetworks(false, true);
    Serial.printf("battery_keepalive_scan count=%d elapsed_ms=%lu\n",
                  network_count,
                  millis() - started_ms);
    WiFi.scanDelete();
  }
#endif
  const unsigned long elapsed_ms = millis() - started_ms;
  if (elapsed_ms < duration_ms) {
    delay(duration_ms - elapsed_ms);
  }
#if LAUNDRY_KEEPALIVE_WIFI_PULSE
  WiFi.disconnect(true, true);
  WiFi.mode(WIFI_OFF);
#endif
  digitalWrite(kLedPin, LOW);
}

void maybe_active_cycle_load_pulse(DetectorState state, unsigned long *nap_ms) {
  const unsigned long pulse_ms = active_cycle_load_pulse_ms(
      millis(),
      state,
      last_active_load_pulse_ms,
      kTelemetryCadence);
  if (pulse_ms == 0) {
    return;
  }

  const unsigned long remaining_after_ms = *nap_ms > pulse_ms ? *nap_ms - pulse_ms : 0;
  Serial.printf("active_cycle_load_pulse state=%s interval_ms=%lu pulse_ms=%lu nap_before_ms=%lu remaining_after_ms=%lu\n",
                detector_state_to_string(state),
                kTelemetryCadence.active_load_pulse_interval_ms,
                pulse_ms,
                *nap_ms,
                remaining_after_ms);
  power_bank_keepalive_pulse(pulse_ms, remaining_after_ms);
  last_active_load_pulse_ms = millis();
  *nap_ms = remaining_after_ms;
}

void nap(unsigned long nap_ms) {
  if (nap_ms == 0) {
    return;
  }
#if LAUNDRY_USE_LIGHT_SLEEP
  unsigned long remaining_ms = nap_ms;
  while (remaining_ms > 0) {
    const BatteryKeepaliveNap slice =
        next_battery_keepalive_nap(remaining_ms, kTelemetryCadence);
    if (slice.sleep_ms > 0) {
      Serial.printf("sleep mode=light duration_ms=%lu remaining_after_ms=%lu\n",
                    slice.sleep_ms,
                    slice.remaining_after_slice_ms + slice.awake_pulse_ms);
      Serial.flush();
      WiFi.mode(WIFI_OFF);
      esp_sleep_enable_timer_wakeup(static_cast<uint64_t>(slice.sleep_ms) * 1000ULL);
      esp_light_sleep_start();
    }
    if (slice.awake_pulse_ms > 0) {
      power_bank_keepalive_pulse(slice.awake_pulse_ms, slice.remaining_after_slice_ms);
    }
    remaining_ms = slice.remaining_after_slice_ms;
  }
#else
  Serial.printf("sleep mode=active_delay duration_ms=%lu\n", nap_ms);
  Serial.flush();
  delay(nap_ms);
#endif
}

bool connect_wifi() {
  if (WiFi.status() == WL_CONNECTED) {
    Serial.printf("wifi_reuse_connected=true rssi=%d\n", WiFi.RSSI());
    maybe_sync_clock();
    return true;
  }

  WiFi.persistent(false);
  WiFi.disconnect(true, true);
  delay(250);
  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);
  WiFi.setMinSecurity(WIFI_AUTH_WPA_PSK);
  WifiTarget target = scan_wifi_target();
  const auto wait_for_connection = [](unsigned long timeout_ms) {
    const unsigned long started = millis();
    while (WiFi.status() != WL_CONNECTED && millis() - started < timeout_ms) {
      delay(250);
    }
    return millis() - started;
  };
  if (target.seen) {
    Serial.printf("wifi_begin target_channel=%d target_rssi=%d\n", target.channel, target.rssi);
    WiFi.begin(WIFI_SSID, WIFI_PASSWORD, target.channel);
  } else {
    Serial.println("wifi_begin target_channel=0 target_unseen=true");
    WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  }
  unsigned long elapsed_ms = wait_for_connection(12000);
  const wl_status_t status = WiFi.status();
  if (status == WL_CONNECTED) {
    Serial.printf("wifi_connected=true rssi=%d elapsed_ms=%lu\n",
                  WiFi.RSSI(),
                  elapsed_ms);
    maybe_sync_clock();
    return true;
  }
  if (target.seen) {
    Serial.printf("wifi_channel_connect_failed=true status=%d elapsed_ms=%lu fallback=generic\n",
                  static_cast<int>(status),
                  elapsed_ms);
    WiFi.disconnect(false, false);
    delay(250);
    WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
    elapsed_ms = wait_for_connection(12000);
    if (WiFi.status() == WL_CONNECTED) {
      Serial.printf("wifi_connected=true mode=generic rssi=%d elapsed_ms=%lu\n",
                    WiFi.RSSI(),
                    elapsed_ms);
      maybe_sync_clock();
      return true;
    }
  }
  Serial.printf("wifi_connected=false status=%d elapsed_ms=%lu\n",
                static_cast<int>(WiFi.status()),
                elapsed_ms);
  log_target_wifi_scan();
  return false;
}

bool post_motion_started_event(uint32_t event_counter, float motion_mg) {
  if (!connect_wifi()) {
    Serial.println("motion_post wifi_failed=true");
    maybe_sleep_wifi();
    return false;
  }

  JsonDocument doc;
  const String event_id = String("motion-") + boot_id + "-" + String(event_counter) + "-" + String(millis());
  doc["device_id"] = DEVICE_ID;
  doc["event_id"] = event_id;
  doc["cycle_id"] = "motion-request-test";
  doc["state"] = "motion_started";
  doc["cycle_label"] = "unknown";
  doc["motion_rms_mg"] = motion_mg;
  doc["last_motion_ms"] = 0;
  doc["firmware_version"] = FIRMWARE_VERSION;
  const String device_time = device_time_utc();
  if (device_time.length() > 0) {
    doc["device_time_utc"] = device_time;
  }

  String body;
  serializeJson(doc, body);

  HTTPClient http;
  http.begin(RELAY_URL);
  http.addHeader("content-type", "application/json");
  http.addHeader("x-laundry-signature", hmac_sha256(body));
  blink_transmit_led();
  const int status = http.POST(body);
  Serial.printf("motion_post status=%d event_id=%s motion_mg=%.1f\n",
                status,
                event_id.c_str(),
                motion_mg);
  http.end();
  maybe_sleep_wifi();
  return status >= 200 && status < 300;
}

bool post_calibration_sample_event(uint32_t event_counter,
                                   const String &run_id,
                                   const MotionWindow &window,
                                   time_t sample_epoch_seconds) {
  if (!connect_wifi()) {
    Serial.println("calibration_post wifi_failed=true");
    maybe_sleep_wifi();
    return false;
  }

  JsonDocument doc;
  const String event_id = run_id + String("-sample-") + String(event_counter) + "-" + String(window.at_ms);
  doc["device_id"] = DEVICE_ID;
  doc["event_id"] = event_id;
  doc["cycle_id"] = run_id;
  doc["state"] = "calibration_sample";
  doc["cycle_label"] = "unknown";
  doc["motion_rms_mg"] = window.rms_mg;
  doc["last_motion_ms"] = 0;
  doc["firmware_version"] = FIRMWARE_VERSION;
  doc["peak_mg"] = window.peak_mg;
  doc["sample_window_ms"] = kSampleWindowMs;
  doc["sample_count"] = kSampleWindowMs / kSampleIntervalMs;
  doc["sensor_type"] = motion_sensor_to_string(motion_sensor);
  doc["uptime_ms"] = window.at_ms;
  doc["wifi_rssi"] = WiFi.status() == WL_CONNECTED ? WiFi.RSSI() : -127;
  const String device_time = format_utc(sample_epoch_seconds);
  if (device_time.length() > 0) {
    doc["device_time_utc"] = device_time;
  }

  String body;
  serializeJson(doc, body);

  HTTPClient http;
  http.begin(RELAY_URL);
  http.addHeader("content-type", "application/json");
  http.addHeader("x-laundry-signature", hmac_sha256(body));
  blink_transmit_led();
  const int status = http.POST(body);
  Serial.printf("calibration_post status=%d event_id=%s rms_mg=%.1f peak_mg=%.1f rssi=%d\n",
                status,
                event_id.c_str(),
                window.rms_mg,
                window.peak_mg,
                WiFi.status() == WL_CONNECTED ? WiFi.RSSI() : -127);
  http.end();
  maybe_sleep_wifi();
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

#elif LAUNDRY_CALIBRATION_CAPTURE

namespace {
uint32_t calibration_counter = 0;
String calibration_run_id;

void blink_led(uint8_t count, unsigned int on_ms, unsigned int off_ms) {
  for (uint8_t i = 0; i < count; i++) {
    digitalWrite(kLedPin, HIGH);
    delay(on_ms);
    digitalWrite(kLedPin, LOW);
    delay(off_ms);
  }
}
} // namespace

void setup() {
  Serial.begin(115200);
  delay(1000);
  pinMode(kLedPin, OUTPUT);
  digitalWrite(kLedPin, LOW);
  WiFi.mode(WIFI_OFF);

  if (!setup_motion_sensor()) {
    Serial.println("calibration_capture_ready=false sensor_detected=false");
    while (true) {
      blink_led(1, 100, 100);
    }
  }

  boot_id = make_boot_id();
  calibration_run_id = String("calibration-") + String(DEVICE_ID) + "-" + boot_id;
  Serial.printf("sensor_type=%s\n", motion_sensor_to_string(motion_sensor));
  const bool wifi_hot = connect_wifi();
  Serial.printf("calibration_wifi_hot=%s\n", wifi_hot ? "true" : "false");
  Serial.printf("calibration_capture_ready=true run_id=%s sample_window_ms=%lu post_interval_ms=0 wifi_keepalive=%s\n",
                calibration_run_id.c_str(),
                kSampleWindowMs,
                LAUNDRY_KEEP_WIFI_CONNECTED ? "true" : "false");
  blink_led(3, 120, 120);
}

void loop() {
  digitalWrite(kLedPin, HIGH);
  const time_t sample_epoch_seconds = time(nullptr);
  MotionWindow window = sample_motion_window();
  digitalWrite(kLedPin, LOW);
  calibration_counter++;

  Serial.printf("calibration_sample counter=%lu rms_mg=%.1f peak_mg=%.1f\n",
                static_cast<unsigned long>(calibration_counter),
                window.rms_mg,
                window.peak_mg);
  const bool ok = post_calibration_sample_event(
      calibration_counter,
      calibration_run_id,
      window,
      sample_epoch_seconds);
  Serial.printf("calibration_result ok=%s\n", ok ? "true" : "false");
  if (ok) {
    blink_led(1, 250, 100);
  } else {
    blink_led(8, 60, 60);
  }
}

#elif LAUNDRY_MOTION_REQUEST_TEST

namespace {
constexpr float kMotionRequestThresholdMg = 8.0f;

MovementTriggerConfig motion_request_config() {
  MovementTriggerConfig config;
  config.threshold_mg = kMotionRequestThresholdMg;
  config.confirm_motion_ms = 300UL;
  config.cooldown_ms = 5000UL;
  return config;
}

MovementTrigger motion_request_trigger(motion_request_config());
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

void blink_led(uint8_t count, unsigned int on_ms, unsigned int off_ms) {
  for (uint8_t i = 0; i < count; i++) {
    digitalWrite(kLedPin, HIGH);
    delay(on_ms);
    digitalWrite(kLedPin, LOW);
    delay(off_ms);
  }
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

  boot_id = make_boot_id();
  Serial.printf("sensor_type=%s\n", motion_sensor_to_string(motion_sensor));
#if LAUNDRY_KEEP_WIFI_CONNECTED
  const bool wifi_hot = connect_wifi();
  Serial.printf("motion_request_wifi_hot=%s\n", wifi_hot ? "true" : "false");
#else
  log_target_wifi_scan();
#endif
  Serial.printf("motion_request_test_ready=true threshold_mg=%.1f confirm_ms=300 cooldown_ms=5000 wifi_keepalive=%s\n",
                kMotionRequestThresholdMg,
                LAUNDRY_KEEP_WIFI_CONNECTED ? "true" : "false");
  blink_led(2, 120, 120);
}

void loop() {
  const float delta_mg = read_delta_mg();
  const bool should_post = motion_request_trigger.observe(millis(), delta_mg);
  digitalWrite(kLedPin, delta_mg >= kMotionRequestThresholdMg ? HIGH : LOW);
  Serial.printf("motion_sample delta_mg=%.1f active=%s should_post=%s\n",
                delta_mg,
                delta_mg >= kMotionRequestThresholdMg ? "true" : "false",
                should_post ? "true" : "false");

  if (should_post) {
    motion_request_counter++;
    blink_led(6, 60, 60);
    const bool ok = post_motion_started_event(motion_request_counter, delta_mg);
    Serial.printf("motion_result ok=%s\n", ok ? "true" : "false");
    if (ok) {
      blink_led(2, 300, 120);
    } else {
      blink_led(10, 50, 50);
    }
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
    maybe_sleep_wifi();
    return false;
  }

  JsonDocument doc;
  const String event_id = String("button-") + boot_id + "-" + String(button_counter) + "-" + String(millis());
  doc["device_id"] = DEVICE_ID;
  doc["event_id"] = event_id;
  doc["cycle_id"] = "manual-button-test";
  doc["state"] = "button_pressed";
  doc["cycle_label"] = "unknown";
  doc["motion_rms_mg"] = 0.0;
  doc["last_motion_ms"] = 0;
  doc["firmware_version"] = FIRMWARE_VERSION;
  const String device_time = device_time_utc();
  if (device_time.length() > 0) {
    doc["device_time_utc"] = device_time;
  }

  String body;
  serializeJson(doc, body);

  HTTPClient http;
  http.begin(RELAY_URL);
  http.addHeader("content-type", "application/json");
  http.addHeader("x-laundry-signature", hmac_sha256(body));
  blink_transmit_led();
  const int status = http.POST(body);
  Serial.printf("button_post status=%d event_id=%s\n", status, event_id.c_str());
  http.end();
  maybe_sleep_wifi();
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
  boot_id = make_boot_id();
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
  digitalWrite(kLedPin, LOW);

  if (!setup_motion_sensor()) {
    Serial.println("sensor_detected=false");
    while (true) {
      delay(1000);
    }
  }

  WiFi.mode(WIFI_OFF);
  boot_id = make_boot_id();
  Serial.printf("sensor_type=%s\n", motion_sensor_to_string(motion_sensor));
  telemetry_run_id = String("production-") + String(DEVICE_ID) + "-" + boot_id;
  Serial.printf("telemetry_enabled=true run_id=%s sample_window_ms=%lu poll_ms=%lu battery_keep_awake_ms=%lu battery_keep_awake_poll_ms=%lu idle_poll_ms=%lu battery_keepalive_interval_ms=%lu battery_keepalive_pulse_ms=%lu active_load_pulse_interval_ms=%lu active_load_pulse_ms=%lu battery_keepalive_mode=%s classifier=server\n",
                telemetry_run_id.c_str(),
                kSampleWindowMs,
                kTelemetryCadence.running_poll_ms,
                kTelemetryCadence.startup_keep_awake_ms,
                kTelemetryCadence.startup_poll_ms,
                kTelemetryCadence.idle_poll_ms,
                kTelemetryCadence.battery_keepalive_interval_ms,
                kTelemetryCadence.battery_keepalive_pulse_ms,
                kTelemetryCadence.active_load_pulse_interval_ms,
                kTelemetryCadence.active_load_pulse_ms,
                LAUNDRY_KEEPALIVE_WIFI_PULSE ? "wifi_radio" : "active_delay");
  Serial.println("sensor_detected=true");
}

void loop() {
  const unsigned long sample_started_ms = millis();
  const time_t sample_epoch_seconds = time(nullptr);
  MotionWindow window = sample_motion_window();
  Decision cadence_decision = telemetry_cadence_detector.observe(window);

  Serial.printf("motion rms_mg=%.2f peak_mg=%.2f telemetry_state=%s telemetry_label=%s classification=server\n",
                window.rms_mg,
                window.peak_mg,
                detector_state_to_string(cadence_decision.state),
                cycle_label_to_string(cadence_decision.label));

  telemetry_counter++;
  const bool telemetry_ok = post_calibration_sample_event(
      telemetry_counter,
      telemetry_run_id,
      window,
      sample_epoch_seconds);
  Serial.printf("telemetry_result ok=%s\n", telemetry_ok ? "true" : "false");

  const unsigned long target_interval_ms = telemetry_poll_ms(
      millis(),
      cadence_decision.state,
      kTelemetryCadence);
  unsigned long nap_ms = nap_duration_ms(sample_started_ms, millis(), target_interval_ms);
  const time_t now_epoch_seconds = time(nullptr);
  const bool wall_clock_aligned = clock_synced && clock_is_valid(now_epoch_seconds);
  if (wall_clock_aligned) {
    nap_ms = aligned_wall_clock_nap_ms(
        static_cast<long>(now_epoch_seconds),
        target_interval_ms);
  }
  Serial.printf("target_interval_ms=%lu next_nap_ms=%lu wall_clock_aligned=%s battery_keep_awake=%s classifier=server\n",
                target_interval_ms,
                nap_ms,
                wall_clock_aligned ? "true" : "false",
                battery_keep_awake_active() ? "true" : "false");
  maybe_active_cycle_load_pulse(cadence_decision.state, &nap_ms);
  nap(nap_ms);
}

#endif
