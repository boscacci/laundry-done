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
and power support through the quiet completion window after that deadline. If it is
still idle at the deadline, the device allows the bank to shut off; a later start
may therefore require pressing the bank button again. The five-minute allowance
is not a claim about the washer's fill time.

`startup_keep_awake` now means only continuous radio wakefulness (the first thirty
seconds), not the full initial-start allowance. After settling, successful light
sleep should report `last_light_sleep_result: 0`; keepalive counts should increase
while waiting for motion. This replaces the fifteen-minute continuous-radio
policy. Actual bank power consumption and minimum keepalive load are unmeasured;
USB tests cannot validate either. Logs distinguish requested sleep from reboots.

## Active-cycle power recovery (2026-09-09)

During motion confirmation, running and the quiet completion window, firmware
now retains connected Wi-Fi with modem sleep disabled and uses awake delays
instead of light sleep. Periodic radio pulses are bypassed in those states so
their cleanup cannot turn the radio off. Normal signed telemetry supplies real
network activity at the existing cadence; connection failures still retry.
Idle/done retain the existing power-saving and eventual bank-shutdown policy.
Diagnostics report `active_continuous_power` to distinguish this policy from
the older pulse counter, which no longer advances during an active cycle.

This deliberately spends more battery to remove long low-draw gaps. The old
nominal 25-second pulse interval was measured from pulse completion, not start;
with eight-second pulses and loop delays, observed completion intervals were
33–46 seconds during the failed running session. Neither radio-on time nor
this recovery mode proves a specific current draw or guarantees bank retention.
Validate on the battery bank, not just USB; voltage/current remain unmeasured.

The owner explicitly requested an immediate OTA recovery during the running
dryer cycle on 2026-09-09, accepting interruption of monitoring. This is a
one-off device-maintenance exception to the normal between-loads rule, not
authorization to redeploy the relay. Build with the existing device configuration,
run native regressions, retain the source commit, and require signed post-boot
telemetry matching the staged image hash before calling installation successful.

Recovery installation was confirmed by signed telemetry at 2026-09-09 03:29:25
UTC: source commit `f1ee837`, build `Sep 9 2026 03:28:43`, OTA job
`112de2fcc0dc4fdc8cda586302bc4472`, and running-image SHA-256
`a1c260cd027903ad3a52d401ff4d6b4151db7daecf53cdcda1f29a0056a3d7bc`.
The device reported the expected software reboot, retained OTA capability, then
enabled `active_continuous_power` after settling. The pinned ESP32 build, all
38 native tests and 102 relay tests passed locally. This confirms installation
and initial operation, not a complete-cycle battery-retention test.

For the first flash, connect the ESP32 by USB to the laptop. After it is flashed,
return it to the battery bank for remote diagnostics. USB-powered tests alone
cannot establish whether the bank will stay on. After the OTA-capable bootstrap
flash, firmware updates can also travel over Wi-Fi. There is no inbound debugger
or Bluetooth service to keep awake. See [Wi-Fi firmware updates](wifi-firmware-updates.md).

Reference: [Espressif reset reasons](https://docs.espressif.com/projects/esp-idf/en/v4.4.5/esp32/api-reference/system/system.html).
