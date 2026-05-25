# Route53 Tailnet-Only Custom Domain

This publishes the laundry monitor at:

```text
https://laundry.robertboscacci.com
```

The DNS record intentionally points at the Optiplex Tailscale IPv4 address:

```text
100.124.5.39
```

That address is only reachable from devices on the tailnet. TLS is terminated by
the local Caddy reverse proxy in `compose.yaml`; Caddy obtains and renews the
certificate with Route53 DNS-01 validation.

## Apply

Authenticate to AWS with permission to manage the public Route53 hosted zone,
then run:

```bash
cd infra/route53-cloudfront
terraform init
terraform apply
```

## Runtime

On the Optiplex:

```bash
docker compose up -d --build caddy
tailscale funnel --https=443 off
tailscale serve --bg --yes --tcp=443 tcp://127.0.0.1:8444
```

Keep Funnel off for this monitor. Tailscale membership is the access control.
