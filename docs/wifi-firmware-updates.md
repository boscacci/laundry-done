# On-demand Wi-Fi firmware updates

One USB bootstrap flash installs the receiver. Future application updates are
discovered in the response to an ordinary signed telemetry check-in. No permanent
radio connection, extra polling loop, inbound port, Bluetooth pairing, cloud
subscription, or Tailscale client on the ESP32 is required.

Only stage updates deliberately, between loads, with stable power. A successful
update reboots the sensor and resets the server's current monitoring session.
Do not update during a wash: the software defers while its local detector reports
motion-confirming, running, or quiet-candidate, but an idle reading cannot prove
the washer is not filling. An update cannot wake an unpowered battery bank.

## Operator commands

Build and test `esp32dev`, using the existing ignored Arduino configuration.
The packaging command uses the Python conda environment with
`server/requirements-dev.txt` installed. Run from `server` so `src` can be added
to `PYTHONPATH`, or set `PYTHONPATH=server/src` from the repository root.

```powershell
pio run -e esp32dev
$env:PYTHONPATH = 'server/src'
conda run -n sr python -m laundry_done_relay.firmware_update `
  --relay http://192.168.1.207:8088 `
  --config firmware/include/laundry_config.h `
  --firmware .pio/build/esp32dev/firmware.bin
```

`--config` reads the existing secret without printing it; alternatively supply
`DEVICE_SECRET` in the process environment. Never pass the secret as a CLI argument.
Do not publish the `.bin`: it contains the Wi-Fi credentials and device key.
The packager encrypts in memory and sends only ciphertext to the relay.

The command prints a public job ID. Check it with:

```powershell
conda run -n sr python -m laundry_done_relay.firmware_update `
  --relay http://192.168.1.207:8088 `
  --config firmware/include/laundry_config.h --status JOB_ID
```

Jobs expire after ten minutes by default (`--ttl-seconds`, 60–3600). They are
offered only after the initial thirty-second settling period and while the local
detector is idle or done. Discovery takes the next successful normal check-in:
about ten seconds while waiting for a start, up to two minutes in long-idle mode.
There is no guarantee of delivery if the bank switches off first.

For deliberate bench maintenance only, `--interrupt-monitoring` overrides the
motion-state deferral. This is an authenticated per-job choice, off by default;
it still waits until the initial thirty-second settling period ends. Use it only
after explicitly deciding it is safe to interrupt the current monitoring session.

`installed` requires a new telemetry report with the expected running-image
SHA-256, not just a completed download. Wireless diagnostics include the build
identifier, job ID, result, and installed hash. An unsuccessful attempt reports
`failed` and resumes monitoring; it is not retried automatically, even across a
reboot. To deliberately retry, stage a new job. Repeating the same staging
request is idempotent; staging a new job supersedes a previous pending one.

## Security and recovery

- Packages use AES-256-GCM with fresh random 96-bit nonces. The encryption key is
  domain-separated from the existing device key using HMAC-SHA256.
- Staging, offers, and downloads have separate HMAC domains. An offer is bound to
  the device and the exact current telemetry event, and has a bounded expiry.
- Ciphertext size is bounded by the existing 1.25 MiB application slot. Streaming
  uses fixed-size buffers and a two-minute download budget plus connection/read
  timeouts. No URLs supplied in the offer are followed; the firmware downloads
  only from its configured relay origin, without redirects.
- The ESP32 writes the inactive slot and checks the GCM authentication tag and
  plaintext SHA-256 before calling `Update.end(false)` to activate it. It verifies
  the running partition hash again after reboot before acknowledging installation.
- Update blobs are encrypted in the relay database. Terminal/superseded jobs drop
  the blob; staging prunes jobs expired for over a day. The queue is bounded to
  twenty retained jobs. Update HTTP responses use `Cache-Control: no-store`.

The current partition remains untouched during a failed transfer. This is not
automatic boot-health rollback: correctly authenticated firmware that cannot boot
or reconnect may still require USB recovery. This also does not provide secure
boot, protect credentials from physical flash access, or rotate the Wi-Fi/device
key. Keep a known-good source revision and retain USB recovery access.

Bluetooth updates are not implemented. Wi-Fi uses the device's existing network
and relay and can work without standing next to the washer.

References: [Espressif Update library](https://github.com/espressif/arduino-esp32/tree/2.0.17/libraries/Update),
[AES-GCM documentation](https://cryptography.io/en/latest/hazmat/primitives/aead/#cryptography.hazmat.primitives.ciphers.aead.AESGCM).
