# Build Guide

## 1. Bench Wire The Sensor

Start on the desk before anything goes near the appliance.

| LIS3DH | ESP32 DevKit |
| --- | --- |
| `VIN` or `3V` | `3V3` |
| `GND` | `GND` |
| `SDA` | GPIO `21` |
| `SCL` | GPIO `22` |
| `INT1` | GPIO `33` optional, reserved for later wake work |

Use Dupont wires first. After the firmware sees the sensor, solder the four
required wires and cover joints with heat-shrink.

## 2. Configure Firmware

Copy the local config template:

```bash
cp firmware/include/laundry_config.h.example firmware/include/laundry_config.h
```

Edit:

- `WIFI_SSID`
- `WIFI_PASSWORD`
- `RELAY_URL`, using the static LAN IP for `optiplex-lan`
- `DEVICE_SECRET`, matching `.env`

Build and upload:

```bash
pio run -e esp32dev -t upload
pio device monitor -b 115200
```

Expected startup log:

```text
sensor_detected=true
```

## 3. Deploy The Relay On optiplex-lan

Copy the repo to the server, then create `.env`:

```bash
cp .env.example .env
```

Edit:

```text
DEVICE_SECRET=<same long secret as firmware laundry_config.h>
GOTIFY_URL=http://gotify:80
GOTIFY_APP_TOKEN=<token from Gotify application settings>
```

Start services:

```bash
docker compose up -d --build
```

Open Gotify at:

```text
http://<optiplex-lan-static-ip>:8089
```

Create an application token in Gotify and paste it into `.env`, then restart:

```bash
docker compose restart relay
```

Health check:

```bash
curl http://<optiplex-lan-static-ip>:8088/healthz
```

## 4. Mount The Puck

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

## 5. Calibrate With Real Cycles

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

- Active: `30 mg`
- Quiet: `12 mg`
- Washer spin peak: `120 mg`
- Done quiet period: `10 minutes`

Only tune after observing real logs. If the dryer never crosses active motion,
lower `active_threshold_mg` in `DetectorConfig`. If foot traffic or door bumps
start cycles, raise it slightly or mount the puck lower on the appliance body.

## 6. Expected Alerts

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
