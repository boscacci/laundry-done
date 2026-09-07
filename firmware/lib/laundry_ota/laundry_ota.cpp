#include "laundry_ota.h"
#include <HTTPClient.h>
#include <Preferences.h>
#include <Update.h>
#include <esp_ota_ops.h>
#include <mbedtls/gcm.h>
#include <mbedtls/md.h>
#include <mbedtls/sha256.h>
#include <time.h>

namespace laundry_ota {
namespace {
Preferences preferences;
bool storage_ready = false;
String last_job, last_result, installed_sha256;
constexpr size_t kMaxImageSize = 0x140000;
constexpr unsigned long kUpdateTimeoutMs = 120000;

String hex(const uint8_t *bytes, size_t length) {
  String result;
  result.reserve(length * 2);
  for (size_t i = 0; i < length; ++i) {
    char pair[3];
    snprintf(pair, sizeof(pair), "%02x", bytes[i]);
    result += pair;
  }
  return result;
}

bool unhex(const String &text, uint8_t *bytes, size_t length) {
  if (text.length() != length * 2) return false;
  for (size_t i = 0; i < length; ++i) {
    uint8_t value = 0;
    for (size_t n = 0; n < 2; ++n) {
      const char ch = text[i * 2 + n];
      if (ch >= '0' && ch <= '9') value = value * 16 + ch - '0';
      else if (ch >= 'a' && ch <= 'f') value = value * 16 + ch - 'a' + 10;
      else return false;
    }
    bytes[i] = value;
  }
  return true;
}

bool equal_bytes(const uint8_t *a, const uint8_t *b, size_t length) {
  uint8_t difference = 0;
  for (size_t i = 0; i < length; ++i) difference |= a[i] ^ b[i];
  return difference == 0;
}

String mac(const char *secret, const String &message) {
  uint8_t digest[32];
  if (mbedtls_md_hmac(mbedtls_md_info_from_type(MBEDTLS_MD_SHA256),
      reinterpret_cast<const uint8_t *>(secret), strlen(secret),
      reinterpret_cast<const uint8_t *>(message.c_str()), message.length(), digest) != 0) return String();
  return hex(digest, sizeof(digest));
}

// One atomic NVS record, public metadata only; never automatically retry a job.
bool save_attempt(const String &job, const String &result, const String &sha, size_t size) {
  JsonDocument record;
  record["job"] = job;
  record["result"] = result;
  record["sha256"] = sha;
  record["size"] = size;
  String value;
  serializeJson(record, value);
  return storage_ready && preferences.putString("attempt", value) == value.length();
}

String running_image_hash(size_t size) {
  const esp_partition_t *partition = esp_ota_get_running_partition();
  if (!partition || size == 0 || size > partition->size) return String();
  mbedtls_sha256_context hash;
  mbedtls_sha256_init(&hash);
  mbedtls_sha256_starts_ret(&hash, 0);
  uint8_t buffer[1024];
  bool ok = true;
  for (size_t offset = 0; offset < size && ok; offset += sizeof(buffer)) {
    const size_t count = min(sizeof(buffer), size - offset);
    ok = esp_partition_read(partition, offset, buffer, count) == ESP_OK &&
         mbedtls_sha256_update_ret(&hash, buffer, count) == 0;
    delay(1);
  }
  uint8_t digest[32];
  ok = ok && mbedtls_sha256_finish_ret(&hash, digest) == 0;
  mbedtls_sha256_free(&hash);
  return ok ? hex(digest, sizeof(digest)) : String();
}

bool download(const JsonDocument &manifest, const String &relay_url,
              const char *secret, const uint8_t *nonce, const uint8_t *expected_tag,
              const uint8_t *expected_sha) {
  const String job = manifest["job_id"].as<String>();
  const size_t size = manifest["size"].as<unsigned long>();
  String origin = relay_url;
  const String event_path = "/api/v1/events";
  if (!origin.endsWith(event_path)) return false;
  origin.remove(origin.length() - event_path.length());
  HTTPClient http;
  http.setConnectTimeout(5000);
  http.setTimeout(5000);
  http.setFollowRedirects(HTTPC_DISABLE_FOLLOW_REDIRECTS);
  if (!http.begin(origin + "/api/v1/firmware/" + job)) return false;
  http.addHeader("x-laundry-signature", mac(secret, "ota-download-v1\n" + job));
  if (http.GET() != 200 || http.getSize() != static_cast<int>(size)) {
    http.end();
    return false;
  }
  uint8_t key[32];
  if (!unhex(mac(secret, "laundry-ota-aes-gcm-v1"), key, sizeof(key))) {
    http.end();
    return false;
  }
  const String aad = "laundry-ota-v1\n" + job + "\n" + manifest["device_id"].as<String>() +
      "\n" + String(size) + "\n" + manifest["sha256"].as<String>();
  mbedtls_gcm_context cipher;
  mbedtls_sha256_context hash;
  mbedtls_gcm_init(&cipher);
  mbedtls_sha256_init(&hash);
  bool ok = mbedtls_gcm_setkey(&cipher, MBEDTLS_CIPHER_ID_AES, key, 256) == 0 &&
      mbedtls_gcm_starts(&cipher, MBEDTLS_GCM_DECRYPT, nonce, 12,
          reinterpret_cast<const uint8_t *>(aad.c_str()), aad.length()) == 0 &&
      mbedtls_sha256_starts_ret(&hash, 0) == 0 && Update.begin(size, U_FLASH);
  memset(key, 0, sizeof(key));
  uint8_t encrypted[1024], plaintext[1024];
  size_t received = 0;
  unsigned next_progress = 25;
  const unsigned long started = millis();
  WiFiClient *stream = http.getStreamPtr();
  stream->setTimeout(5000);
  while (ok && received < size) {
    const size_t count = min(sizeof(encrypted), size - received);
    // Full blocks except the final one, as required by this core's GCM API.
    const size_t got = stream->readBytes(encrypted, count);
    ok = got == count && millis() - started < kUpdateTimeoutMs &&
         mbedtls_gcm_update(&cipher, count, encrypted, plaintext) == 0 &&
         mbedtls_sha256_update_ret(&hash, plaintext, count) == 0 &&
         Update.write(plaintext, count) == count;
    if (!ok) break;
    received += count;
    const unsigned progress = received * 100 / size;
    if (progress >= next_progress) {
      Serial.printf("ota_progress percent=%u\n", progress);
      next_progress += 25;
    }
    delay(1);
  }
  uint8_t tag[16], digest[32];
  ok = ok && received == size && mbedtls_gcm_finish(&cipher, tag, sizeof(tag)) == 0 &&
       mbedtls_sha256_finish_ret(&hash, digest) == 0 &&
       equal_bytes(tag, expected_tag, sizeof(tag)) && equal_bytes(digest, expected_sha, sizeof(digest));
  // Keep the current slot untouched. Activate only after authentication and hash pass.
  if (ok) ok = save_attempt(job, "verified", manifest["sha256"].as<String>(), size);
  if (ok) ok = Update.end(false);
  if (!ok) Update.abort();
  mbedtls_gcm_free(&cipher);
  mbedtls_sha256_free(&hash);
  memset(plaintext, 0, sizeof(plaintext));
  http.end();
  return ok;
}
} // namespace

void begin() {
  storage_ready = preferences.begin("laundry-ota", false);
  if (!storage_ready) return;
  JsonDocument record;
  if (deserializeJson(record, preferences.getString("attempt", "{}"))) return;
  last_job = record["job"] | "";
  if (last_job.isEmpty()) return;
  last_result = "failed";
  if (record["result"] == "verified") {
    const String actual = running_image_hash(record["size"] | 0UL);
    if (!actual.isEmpty() && actual == record["sha256"].as<String>()) {
      last_result = "installed";
      installed_sha256 = actual;
    }
  }
}

void diagnostics(JsonObject destination) {
  destination["ota_capable"] = storage_ready;
  destination["firmware_build"] = __DATE__ " " __TIME__;
  destination["ota_job_id"] = last_job;
  destination["ota_result"] = last_result;
  if (!installed_sha256.isEmpty()) destination["installed_sha256"] = installed_sha256;
}

void handle_offer(JsonVariantConst offer, const String &event_id, bool allowed,
                  const char *relay_url, const char *device_id, const char *secret) {
  if (!allowed || !storage_ready || !offer.is<JsonObjectConst>()) return;
  const String text = offer["manifest"] | "";
  const String signature = offer["signature"] | "";
  if (text.length() > 1536) return;
  uint8_t supplied_mac[32], calculated_mac[32];
  if (!unhex(signature, supplied_mac, sizeof(supplied_mac)) ||
      !unhex(mac(secret, "ota-offer-v1\n" + text), calculated_mac, sizeof(calculated_mac)) ||
      !equal_bytes(supplied_mac, calculated_mac, sizeof(supplied_mac))) {
    Serial.println("ota_rejected reason=signature");
    return;
  }
  JsonDocument manifest;
  if (deserializeJson(manifest, text)) return;
  const String job = manifest["job_id"] | "";
  uint8_t nonce[12], tag[16], sha[32], job_bytes[16];
  const time_t now = time(nullptr);
  if (manifest["version"].as<int>() != 1 || manifest["device_id"].as<String>() != device_id ||
      manifest["event_id"].as<String>() != event_id || !manifest["size"].is<unsigned long>() ||
      !manifest["expires_at"].is<unsigned long>() || now < 1700000000 ||
      manifest["expires_at"].as<unsigned long>() < now ||
      manifest["expires_at"].as<unsigned long>() - now > 3600 ||
      manifest["size"].as<unsigned long>() == 0 || manifest["size"].as<unsigned long>() > kMaxImageSize ||
      !unhex(job, job_bytes, sizeof(job_bytes)) || job == last_job ||
      !unhex(manifest["nonce"] | "", nonce, sizeof(nonce)) ||
      !unhex(manifest["tag"] | "", tag, sizeof(tag)) ||
      !unhex(manifest["sha256"] | "", sha, sizeof(sha))) {
    Serial.println("ota_rejected reason=metadata_or_replay");
    return;
  }
  if (!save_attempt(job, "attempting", "", 0)) return;
  last_job = job;
  last_result = "failed";
  Serial.printf("ota_started bytes=%lu timeout_ms=%lu\n",
                manifest["size"].as<unsigned long>(), kUpdateTimeoutMs);
  if (download(manifest, relay_url, secret, nonce, tag, sha)) {
    Serial.println("ota_verified rebooting=true");
    Serial.flush();
    delay(100);
    ESP.restart();
  }
  save_attempt(job, "failed", "", 0);
  Serial.println("ota_failed monitoring_resumed=true");
}
} // namespace laundry_ota
