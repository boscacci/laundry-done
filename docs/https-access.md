# HTTPS Access Through Tailscale

The normal dashboard URL is:

```text
https://laundry.robertboscacci.com/monitor
```

The stable Gotify mobile/server URL is:

```text
https://gotify.robertboscacci.com
```

That hostname is public DNS, but it points at the Optiplex Tailscale IPv4
address (`100.124.5.39`). The address is not routable from the public internet,
so the browser must be on the tailnet. Tailscale membership is the dashboard
authentication layer.

## Traffic Split

| Traffic | URL style | Why |
| --- | --- | --- |
| ESP32 to relay | `http://<home-server-lan-ip>:8088/api/v1/events` | Simple local HTTP, signed with HMAC. |
| Browser dashboard | `https://laundry.robertboscacci.com/monitor` | Real HTTPS cert, reachable only from tailnet devices. |
| Gotify app/UI | `https://gotify.robertboscacci.com` | Real HTTPS cert, reachable only from tailnet devices. |
| Local Gotify port | `127.0.0.1:8089` on the home server | Localhost-only fallback; not exposed to the LAN. |

## Runtime Shape

Caddy terminates HTTPS locally and renews the certificate with Route53 DNS-01:

```bash
docker compose up -d --build caddy
```

Tailscale forwards tailnet TCP port 443 to that local Caddy listener:

```bash
tailscale funnel --https=443 off
tailscale serve --bg --yes --tcp=80 tcp://127.0.0.1:8081
tailscale serve --bg --yes --tcp=443 tcp://127.0.0.1:8444
```

Expected status:

```text
|-- tcp://optiplex.<tailnet>.ts.net:80 (TCP, tailnet only)
|--> tcp://127.0.0.1:8081
|-- tcp://optiplex.<tailnet>.ts.net:443 (TLS over TCP, tailnet only)
|--> tcp://127.0.0.1:8444
```

Port 80 exists only to catch accidental `http://` browser visits and let Caddy
redirect them to HTTPS. Without this tailnet-only forward, plain HTTP reaches
Pi-hole on the host and shows Pi-hole's access-denied page.

## Warm-Up Worker

The Compose stack includes a `warmup` worker that periodically probes Caddy
with the same URLs used by mobile devices:

```text
https://laundry.robertboscacci.com/healthz
https://gotify.robertboscacci.com/health
```

By default it runs every 5 minutes with a 10-second timeout. It does not send
Gotify messages and does not need any app tokens. The worker supports TLS
verification, but the Compose default disables it for this tailnet-only path
because the home-server-side route can present a self-signed chain even though
client devices may already trust it. Docker network aliases point those
hostnames at the Caddy container from inside the Compose network, so SNI, Host,
and Caddy routing match the mobile-facing route without depending on same-host
Tailscale hairpin behavior. Its logs are structured JSON records with query
strings stripped from URLs, so future URL changes do not leak secret parameters
into logs.

Override the defaults in `.env` only when the private hostnames or cadence
change:

```text
WARMUP_URLS=https://laundry.robertboscacci.com/healthz,https://gotify.robertboscacci.com/health
WARMUP_HOST_HEADERS=
WARMUP_INTERVAL_SECONDS=300
WARMUP_TIMEOUT_SECONDS=10
WARMUP_VERIFY_TLS=false
```

Check the worker after deploy:

```bash
docker compose logs --tail=20 warmup
```

## DNS

Route53 should have a plain `A` record:

```text
laundry.robertboscacci.com.  A  100.124.5.39
gotify.robertboscacci.com.   A  100.124.5.39
```

There should be no public CloudFront alias for this monitor and no public Funnel
on port 443.

## Security Notes

- The monitor relies on Tailscale access, not a dashboard password.
- Gotify's human login can stay intentionally low-friction because the raw
  Gotify port is localhost-only and the friendly URL points at a Tailscale-only
  address.
- The ESP32 event ingest endpoint still requires signed payloads with
  `DEVICE_SECRET`.
- Keep `GOTIFY_URL` in `.env` as `http://gotify:80`; that is the relay's
  internal Docker-to-Docker URL.
- If a device cannot load the page, first confirm that Tailscale is connected on
  that device, then retry the hostname after DNS cache expiry.

## Disable

```bash
tailscale serve --tcp=80 off
tailscale serve --tcp=443 off
```
