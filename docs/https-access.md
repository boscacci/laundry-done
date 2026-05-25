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

## Runtime Shape

Caddy terminates HTTPS locally and renews the certificate with Route53 DNS-01:

```bash
docker compose up -d --build caddy
```

Tailscale forwards tailnet TCP port 443 to that local Caddy listener:

```bash
tailscale funnel --https=443 off
tailscale serve --bg --yes --tcp=443 tcp://127.0.0.1:8444
```

Expected status:

```text
|-- tcp://optiplex.<tailnet>.ts.net:443 (TLS over TCP, tailnet only)
|--> tcp://127.0.0.1:8444
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
- The ESP32 event ingest endpoint still requires signed payloads with
  `DEVICE_SECRET`.
- Keep `GOTIFY_URL` in `.env` as `http://gotify:80`; that is the relay's
  internal Docker-to-Docker URL.
- If a device cannot load the page, first confirm that Tailscale is connected on
  that device, then retry the hostname after DNS cache expiry.

## Disable

```bash
tailscale serve --tcp=443 off
```
