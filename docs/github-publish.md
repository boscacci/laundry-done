# Publishing To GitHub

This project is written to work as a portfolio piece or instructable.

Suggested repository name:

```text
laundry-done
```

Suggested description:

```text
ESP32 vibration detector for washer/dryer finish notifications, with a Dockerized Gotify relay.
```

Recommended GitHub topics:

```text
esp32, arduino, platformio, fastapi, gotify, home-automation, iot, laundry
```

Suggested pinned README blurb:

```text
Battery-friendly ESP32 sensor puck that watches washer/dryer vibration from the outside, streams calibration data to a local dashboard, and sends Gotify phone alerts when laundry is done.
```

Good files to highlight in the project description:

- `docs/instructable.md` for the step-by-step article draft.
- `docs/architecture.md` for diagrams.
- `docs/parts-guide.md` for order notes.
- `server/src/laundry_done_relay/app.py` for the relay and dashboard.
- `firmware/src/main.cpp` for the ESP32 firmware.

After logging in with GitHub CLI:

```bash
gh auth login
gh repo create laundry-done --public --source=. --remote=origin --push
```

Do not commit:

- `.env`
- `firmware/include/laundry_config.h`
- `data/`
- `.pio/`
- `.pytest_cache/`
- `__pycache__/`

Before publishing, scan docs for personal hostnames, private IPs, screenshots
with passwords, or anything that identifies your home network. Use placeholders
such as `<home-server-lan-ip>` in public instructions.
