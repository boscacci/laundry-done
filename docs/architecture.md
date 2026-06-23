# Architecture Diagrams

These diagrams are intentionally Mermaid-first so they render on GitHub and can
be copied into Instructables, FigJam, Excalidraw, or a blog post.

## System Overview

```mermaid
flowchart LR
  washer["Washer/dryer body"] -->|"Vibration"| puck["ESP32 sensor puck"]
  puck -->|"I2C samples"| accel["LSM6DS3 or LIS3DH"]
  puck -->|"Signed HTTP event over Wi-Fi"| relay["FastAPI relay"]
  relay -->|"Calibration samples"| sqlite["SQLite event log"]
  relay -->|"Finished-cycle alert"| gotify["Gotify server"]
  gotify -->|"Push notification"| phone["Android phone"]
  relay -->|"Live chart API"| monitor["Dashboard at /monitor"]
  tailscale["Tailscale Serve HTTPS"] -->|"Private HTTPS"| monitor
  tailscale -->|"Private HTTPS on 8443"| gotify
```

## Wiring

```mermaid
flowchart LR
  esp3v3["ESP32 3V3"] -->|"Power"| vin["Accelerometer VIN or VCC"]
  espGnd["ESP32 GND"] -->|"Ground"| gnd["Accelerometer GND"]
  espSda["ESP32 GPIO 21"] -->|"I2C SDA"| sda["Accelerometer SDA"]
  espScl["ESP32 GPIO 22"] -->|"I2C SCL"| scl["Accelerometer SCL"]
  int1["Accelerometer INT1 (optional)"] -.->|"Reserved for future wake"| gpio33["ESP32 GPIO 33"]
```

## Firmware State Machine

```mermaid
stateDiagram-v2
  [*] --> Idle
  Idle --> MotionConfirming: active motion starts
  MotionConfirming --> Idle: motion drops too soon
  MotionConfirming --> CycleRunning: motion remains active
  CycleRunning --> QuietCandidate: quiet begins
  QuietCandidate --> CycleRunning: motion resumes
  QuietCandidate --> DoneSent: quiet long enough
  DoneSent --> MotionConfirming: new motion starts
```

## Telemetry And Dashboard Flow

```mermaid
sequenceDiagram
  participant Sensor as ESP32 sensor puck
  participant Relay as FastAPI relay
  participant Db as SQLite
  participant Browser as Monitor page
  participant Gotify as Gotify
  Sensor->>Relay: Signed calibration_sample
  Relay->>Db: Store raw sample
  Relay->>Relay: Smooth and classify readings
  Relay->>Db: Store server-generated done_sent marker when quiet confirms
  Sensor->>Relay: Signed done_sent when firmware confirms done
  Relay->>Gotify: POST phone alert for first done marker
  Browser->>Relay: GET /api/v1/calibration/events
  Relay->>Db: Read recent samples
  Relay-->>Browser: JSON samples, server phases, notification markers
```

## Dashboard Terms

| Dashboard label | Meaning |
| --- | --- |
| Vibration strength | RMS motion in `mg`, or the typical shake during the sample window. |
| Biggest jolt | Peak motion in `mg`, or the largest instant change in that window. |
| Phase background | Server-side best guess for that time span: quiet, washer, dryer, or strong spin. |
| Sensor sample | Time reported by the ESP32 when it took the measurement. |
| Relay received | Time the home server received the measurement. |
| Wi-Fi signal | ESP32 Wi-Fi RSSI in dBm; less negative is stronger. |

## Why A Relay Exists

The ESP32 could send notifications directly to a cloud service, but the relay
keeps the embedded firmware simple and private:

- The ESP32 only knows the home Wi-Fi, relay URL, and device secret.
- The relay owns Gotify credentials.
- Calibration data stays in a local SQLite database.
- The phone can read the same relay dashboard for tuning.
- Gotify can be kept LAN-only, Tailscale-only, or exposed through a controlled
  tunnel later.
