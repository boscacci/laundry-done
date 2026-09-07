# HTTPS Access

The normal dashboard URL is:

```text
https://laundry.robertboscacci.com/monitor
```

The stable Gotify mobile/server URL is:

```text
https://gotify.robertboscacci.com
```

`laundry.robertboscacci.com` is public DNS, but it points at the Optiplex
Tailscale IPv4 address (`100.124.5.39`). The address is not routable from the
public internet, so dashboard browsers must be on the tailnet. Tailscale
membership is the dashboard authentication layer.

`gotify.robertboscacci.com` is public. Route53 aliases it to CloudFront, and
CloudFront forwards to the public Tailscale Funnel origin:

```text
https://optiplex.tailbea63b.ts.net:10000/gotify
```

## Traffic Split

| Traffic | URL style | Why |
| --- | --- | --- |
| ESP32 to relay | `http://<home-server-lan-ip>:8088/api/v1/events` | Simple local HTTP, signed with HMAC. |
| Browser dashboard | `https://laundry.robertboscacci.com/monitor` | Real HTTPS cert, reachable only from tailnet devices. |
| Gotify app/UI | `https://gotify.robertboscacci.com` | Public CloudFront URL backed by Tailscale Funnel. |
| Local Gotify port | `127.0.0.1:8089` on the home server | Localhost-only fallback. |

## Runtime Shape

Caddy terminates HTTPS locally for the laundry hostname and renews certificates
with Route53 DNS-01:

```bash
docker compose up -d --build caddy
```

The Optiplex tailnet `80/443` front door is shared with other private services.
The live front door is the host-network `optiplex-front-door` nginx container,
configured in:

```bash
/home/rob/optiplex-front-door/nginx.conf
```

It routes by SNI:

```text
laundry.robertboscacci.com  -> 127.0.0.1:8444  # Caddy TLS passthrough
vhf-dev.robertboscacci.com  -> 127.0.0.1:9443  # nginx TLS termination
```

Port 80 exists only to catch accidental `http://` browser visits and redirect
them to HTTPS. HTTPS port `443` must remain free for this nginx listener.

## Gotify Funnel

On the Optiplex, Gotify is exposed publicly through a path on the existing
`10000` Funnel listener:

```bash
tailscale funnel --bg --https=10000 --set-path=/gotify --yes 127.0.0.1:8089
tailscale funnel status
```

Do not use Funnel or Serve on HTTPS `443` for Gotify. If Tailscale owns `443`,
tailnet clients connecting to `laundry.robertboscacci.com` can hit a TLS alert
before the request reaches nginx/Caddy. Firefox reports that as:

```text
SSL_ERROR_INTERNAL_ERROR_ALERT
```

## Warm-Up Worker

The Compose stack includes a `warmup` worker that periodically probes:

```text
https://laundry.robertboscacci.com/healthz
https://gotify.robertboscacci.com/health
```

By default it runs every 5 minutes with a 10-second timeout. It does not send
Gotify messages and does not need app tokens. The worker logs structured JSON
records with query strings stripped from URLs.

Override the defaults in `.env` only when the hostnames or cadence change:

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

Route53 should have:

```text
laundry.robertboscacci.com.  A       100.124.5.39
gotify.robertboscacci.com.   A/AAAA  CloudFront alias
```

## Security Notes

- The monitor relies on Tailscale access, not a dashboard password.
- Gotify's human login and app tokens are internet-facing through CloudFront
  and Funnel; treat them as public-service credentials.
- Open hardening tasks are tracked in [security-todo.md](security-todo.md).
- The ESP32 event ingest endpoint still requires signed payloads with
  `DEVICE_SECRET`.
- Keep `GOTIFY_URL` in `.env` as `http://gotify:80`; that is the relay's
  internal Docker-to-Docker URL.
- If Firefox cannot load `laundry.robertboscacci.com`, first confirm Tailscale
  is connected, then confirm `tailscale funnel status` shows nothing on HTTPS
  `443`.

## Disable Conflicting Funnel

```bash
tailscale funnel --https=443 off
```
