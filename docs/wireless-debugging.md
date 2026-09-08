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
- Detector state, startup settling/keep-awake flags, and whether light sleep is enabled.
- Previous requested nap and last light-sleep return code (`-1` means unattempted).
- Completed keepalive count and uptime at the last completed pulse.
- Successful/failed sample uploads and the previous sample's HTTP status
  (`0` means no HTTP result, including Wi-Fi connection failure).
- Free heap and the lowest free heap observed during this boot.

Counters describe completed work before the current packet and restart at boot.
The diagnostics reflect firmware actions, not measured battery voltage/current.
The measured-noise correction adds `sample_valid` and an actual per-window
`sample_count`; older builds reported a nominal count. Invalid windows cannot
advance the detector's quiet countdown. See [motion classification](motion-classification.md)
for the measured fixtures and deployment gate.
Loss of power prevents a final report. The next successful boot can report its
reset cause; a power-on indication alone does not identify the cable, battery
bank, or reset pin as the cause. There is no voltage/current sensor in this build.

The local detector ignores the first thirty seconds of movement. Only that
settling period keeps the CPU and Wi-Fi continuously awake. It then uses light
sleep between ten-second checks, with periodic bank-load pulses, for up to five
minutes from boot to detect a start. An established cycle retains fast sampling
and keepalive through the quiet completion window after that deadline. If it is
still idle at the deadline, the device allows the bank to shut off; a later start
may therefore require pressing the bank button again. The five-minute allowance
is not a claim about the washer's fill time.

`startup_keep_awake` now means only continuous radio wakefulness (the first thirty
seconds), not the full initial-start allowance. After settling, successful light
sleep should report `last_light_sleep_result: 0`; keepalive counts should increase
while waiting for motion. This replaces the fifteen-minute continuous-radio
policy. Actual bank power consumption and minimum keepalive load are unmeasured;
USB tests cannot validate either. Logs distinguish requested sleep from reboots.

For the first flash, connect the ESP32 by USB to the laptop. After it is flashed,
return it to the battery bank for remote diagnostics. USB-powered tests alone
cannot establish whether the bank will stay on. After the OTA-capable bootstrap
flash, firmware updates can also travel over Wi-Fi. There is no inbound debugger
or Bluetooth service to keep awake. See [Wi-Fi firmware updates](wifi-firmware-updates.md).

Reference: [Espressif reset reasons](https://docs.espressif.com/projects/esp-idf/en/v4.4.5/esp32/api-reference/system/system.html).
