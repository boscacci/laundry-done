# Wireless power diagnostics

The ESP32 already posts vibration, boot/session ID, uptime, and Wi-Fi strength to
the relay. These records survive after the battery bank switches off. One USB
flash of the diagnostic firmware adds a `diagnostics` object to those same signed
requests. It uses the existing reporting cadence and relay authentication.

From the relay checkout on the OptiPlex, inspect the latest packets:

```bash
conda run -n dell python server/src/laundry_done_relay/diagnostics.py \
  --database data/relay/events.sqlite3 --device-id laundry-stack-1 --limit 12
```

Run the command again after each battery-bank experiment. It opens SQLite
read-only, never sends notifications, and works with old firmware too. Old
packets show `reset_reason_name: unavailable` until diagnostic firmware is flashed.

New diagnostic fields include:

- Reset reason: power-on, brownout, watchdog, software restart, or panic.
- Detector state, startup keep-awake flag, and whether light sleep is enabled.
- Previous requested nap and last light-sleep return code (`-1` means unattempted).
- Completed keepalive count and uptime at the last completed pulse.
- Successful/failed sample uploads and the previous sample's HTTP status
  (`0` means no HTTP result, including Wi-Fi connection failure).
- Free heap and the lowest free heap observed during this boot.

Counters describe completed work before the current packet and restart at boot.
The diagnostics reflect firmware actions, not measured battery voltage/current.
Loss of power prevents a final report. The next successful boot can report its
reset cause; a power-on indication alone does not identify the cable, battery
bank, or reset pin as the cause. There is no voltage/current sensor in this build.

The existing startup window is ten minutes, but the production firmware still
turns Wi-Fi off and light-sleeps between samples during that window. Its load
pulses may not satisfy a particular power bank's minimum-load requirements. Logs
help distinguish an intentional long nap from repeated reboots; compare USB
power and battery-bank runs before choosing a power-policy change.

For the first flash, connect the ESP32 by USB to the laptop. After it is flashed,
return it to the battery bank for remote diagnostics. USB-powered tests alone
cannot establish whether the bank will stay on. The current firmware does not
implement OTA updates or an inbound wireless debugger. Adding OTA later requires
an authenticated update path and stable power during flashing.

Reference: [Espressif reset reasons](https://docs.espressif.com/projects/esp-idf/en/v4.4.5/esp32/api-reference/system/system.html).
