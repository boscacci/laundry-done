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
