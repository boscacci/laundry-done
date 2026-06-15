# Build Guide

## 1. Bench Wire The Sensor

Start on the desk before anything goes near the appliance.

| LSM6DS3 or LIS3DH | ESP32 DevKit |
| --- | --- |
| `VIN`, `VCC`, or `3V` | `3V3` |
| `GND` | `GND` |
| `SDA` | GPIO `21` |
| `SCL` | GPIO `22` |
| `INT1`, if present | GPIO `33` optional, reserved for later wake work |

Use Dupont wires first. After the firmware sees the sensor, solder the four
required wires and cover joints with heat-shrink.

## 2. Bench-Test The Accelerometer

If the sensor is wired correctly, the ESP32 can scan I2C before any Wi-Fi setup:

```bash
pio run -e i2c_scan -t upload --upload-port /dev/cu.usbserial-8
pio device monitor --port /dev/cu.usbserial-8 --baud 115200
```

A GODIYMODULES LSM6DS3 breakout normally appears at `0x6B` with register `0x0F`
equal to `0x69`.

Then upload the LED motion test:

```bash
pio run -e accel_led_test -t upload --upload-port /dev/cu.usbserial-8
pio device monitor --port /dev/cu.usbserial-8 --baud 115200
```

Expected startup log:

```text
sensor_type=LSM6DS3
accel_led_test_ready=true sensor_detected=true
```

At rest, `delta_mg` should settle near single digits and `led=4`. Tapping or
tilting the board should spike `delta_mg` and brighten the onboard LED.

To test Wi-Fi and relay notifications from accelerometer motion, upload the
motion request test:

```bash
pio run -e motion_request_test -t upload --upload-port /dev/cu.usbserial-8
pio device monitor --port /dev/cu.usbserial-8 --baud 115200
```

Move the board for about 300 ms. The sketch keeps Wi-Fi connected, posts a signed
`motion_started` event, and then waits 5 seconds before it can post again.

To capture real washer/dryer data for tuning, upload the calibration firmware:

```bash
pio run -e calibration_capture -t upload --upload-port /dev/cu.usbserial-8
pio device monitor --port /dev/cu.usbserial-8 --baud 115200
```

The calibration firmware posts one signed `calibration_sample` event per
4-second motion window. These events do not send phone notifications. Fetch the
latest samples from a Tailscale-connected device:

```bash
curl 'https://laundry.robertboscacci.com/api/v1/calibration/events?limit=200&days=14&max_peak_mg=300'
```

For a live graph, open:

```text
https://laundry.robertboscacci.com/monitor
```

The monitor polls every 2 seconds and plots vibration strength and biggest
jolts from the calibration stream. The short version is: `mg = 1/1000 g`, RMS
is typical shake, and peak is the largest single jolt in the same sample
window. Background colors on the chart are time spans for the dashboard's best
guess, not horizontal threshold lines. Relay data expires after 14 days; each
write or dashboard read prunes older events from the local SQLite database.

The dashboard URL uses HTTPS and is private to the tailnet. See
[HTTPS Access Through Tailscale](https-access.md) for the Route53, Caddy, and
Tailscale Serve setup.

## 3. Configure Firmware

Copy the local config template:

```bash
cp firmware/include/laundry_config.h.example firmware/include/laundry_config.h
```

Edit:

- `WIFI_SSID`
- `WIFI_PASSWORD`
- `RELAY_URL`, using the static LAN IP for your home server
- `DEVICE_SECRET`, matching `.env`

Smart Connect/shared 2.4 GHz + 5 GHz SSIDs are okay. The ESP32 can only join
2.4 GHz, but it scans visible 2.4 GHz APs for the configured SSID and falls back
to a generic SSID/password connect if the strongest scanned channel fails.

Build and upload:

```bash
pio run -e esp32dev -t upload
pio device monitor -b 115200
```

Expected startup log:

```text
sensor_type=LSM6DS3
sensor_detected=true
```

## 4. Deploy The Relay On The Home Server

Copy the repo to the server, then create `.env`:

```bash
cp .env.example .env
```

Edit:

```text
DEVICE_SECRET=<same long secret as firmware laundry_config.h>
GOTIFY_URL=http://gotify:80
GOTIFY_APP_TOKEN=<token from Gotify application settings>
GOTIFY_DEFAULTUSER_PASS=<long random Gotify admin password>
```

Start services:

```bash
docker compose up -d --build
```

Open Gotify at:

```text
http://<home-server-lan-ip>:8089
```

Create an application token in Gotify and paste it into `.env`, then restart:

```bash
docker compose restart relay
```

Health check:

```bash
curl http://<home-server-lan-ip>:8088/healthz
```

## 5. Log In From Android

Install the Gotify Android app from Google Play or F-Droid.

Use these settings:

| Field | Value |
| --- | --- |
| Server URL on home Wi-Fi | `http://<home-server-lan-ip>:8089` |
| Server URL over Tailscale HTTPS | `https://<home-server>.<tailnet>.ts.net:8443` |
| Server URL over Tailscale IP fallback | `http://<home-server-tailscale-ip>:8089` |
| Username | `admin` |
| Password | The `GOTIFY_DEFAULTUSER_PASS` value in `~/repos/laundry-done/.env` on your home server |

To copy the password from your Mac without printing it:

```bash
ssh <home-server> 'cd ~/repos/laundry-done && awk -F= "/^GOTIFY_DEFAULTUSER_PASS=/{print \$2}" .env' | pbcopy
```

If you need to show it in a terminal on the server:

```bash
cd ~/repos/laundry-done
awk -F= '/^GOTIFY_DEFAULTUSER_PASS=/{print $2}' .env
```

## 6. Mount The Puck

Your photos show a stacked GE washer/dryer with a shared cabinet. Use one sensor
puck for v1.

Good mounting spots:

- Flat front or side panel near the seam between washer and dryer.
- Same metal body as the USB power bank.
- Somewhere the USB cable can be short and cannot snag.

Avoid:

- The dangling hook as the primary mount. It can rattle and create false motion.
- Door edges, hinges, the dryer exhaust, and anything warm.
- Cable runs between two parts that move differently.

Recommended physical build:

1. Put ESP32 and accelerometer in a small plastic box.
2. Keep the accelerometer flat against the box wall closest to the appliance.
3. Use Dual Lock, strong tape, or the magnetic power bank plus a strap to keep
   the enclosure pressed flat.
4. Add hot glue or heat-shrink as strain relief where the USB cable enters.

## 7. Calibrate With Real Cycles

Open serial monitor during one washer run and one dryer run:

```bash
pio device monitor -b 115200
```

Watch lines like:

```text
motion rms_mg=34.25 peak_mg=45.10 state=2 label=unknown
event_post status=202 label=washer
```

Default thresholds:

- Active: `3 mg`
- Active peak: `8 mg`
- Quiet: `1.5 mg`
- Washer spin peak: `120 mg`
- Done quiet period: `10 minutes`

These thresholds came from the first bedding-wash calibration run, where real
washer activity lived around `2-5 mg` RMS with `6-16 mg` peaks. A window counts
as active when RMS crosses `3 mg` or peak crosses `8 mg`. If foot traffic or door
bumps start cycles, raise `active_threshold_mg` / `active_peak_threshold_mg`
slightly or mount the puck lower on the appliance body. If the machine finishes
but never alerts, raise `quiet_threshold_mg` slightly after checking
stopped-machine logs.

The production firmware also posts dashboard telemetry as `calibration_sample`
events. These samples feed `/monitor` but do not create Gotify phone
notifications. Production firmware samples a 4-second motion window, then uses
a battery-aware cadence: 10-second samples for the first 10 minutes after boot,
2-minute samples while idle/done after that startup window, and 10-second
samples again while motion or the done-confirmation quiet window is active. The
ESP32 uses a cadence-only motion detector that is intentionally more sensitive
than the relay notification classifier, so quiet washer movement keeps the
board in fast sampling without directly creating phone alerts. During active
cycles, it also runs an 8-second Wi-Fi scan/radio load pulse every 25 seconds.
That leaves margin under the measured sub-40-second no-load cutoff on the
HyperGear 15455 power bank. Post-finish idle/done naps intentionally avoid
keep-alive pulses so the power bank can auto-off after the alert. When NTP is
available, the next sample is aligned to the cadence boundary. It includes an NTP-backed
`device_time_utc` field in telemetry payloads; the relay receive time remains
as a fallback.

The dashboard labels vibration in `mg`, which means milli-g: thousandths of
Earth gravity. A reading of `300 mg` is `0.3 g`. The monitor hides samples whose
peak jolt is above `300 mg` because those are usually handling bumps from moving
or touching the sensor rather than washer/dryer rhythm. Classification happens
on the relay, not on the ESP32: the sensor sends signed readings, and the relay
uses smoothed recent readings to decide phase labels and Gotify alerts.

By default the production firmware uses ESP32 light sleep between samples, but
only startup and active-cycle waits are split into keep-alive slices. After the
startup window, idle/done states take a 2-minute nap with no keep-alive pulse so
the USB battery bank can auto-off. The firmware turns Wi-Fi off between
transmissions after the startup window and returns to 10-second sampling when it
sees machine motion. Once a cycle is active or in the done-confirmation quiet
window, it adds the heavier 8-second Wi-Fi scan/radio pulse every 25 seconds so
the power bank sees a substantial recurring load. The controllable onboard LED
stays off except for transmit blinks and keep-alive pulses. Dashboard filters
handling noise before applying the visible sample limit, so a few large bumps do
not empty the live chart.

## 8. Expected Alerts

Normal sequence:

1. Washer runs.
2. Motion stops for 10 minutes.
3. Android receives `Washer done`.
4. Dryer starts soon after.
5. Motion stops for 10 minutes.
6. Android receives `Dryer done`.

If washer and dryer run together and the one-sensor classifier cannot separate
them, Android receives:

```text
Laundry stack stopped
```
