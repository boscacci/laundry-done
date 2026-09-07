#pragma once
#include <Arduino.h>
#include <ArduinoJson.h>

namespace laundry_ota {
void begin();
void diagnostics(JsonObject destination);
void handle_offer(JsonVariantConst offer, const String &event_id, bool allowed, bool settled,
                  const char *relay_url, const char *device_id, const char *secret);
}
