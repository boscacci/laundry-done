# Route53 and CloudFront Front Doors

This stack publishes the laundry tools at:

```text
https://laundry.robertboscacci.com  # tailnet-only
https://gotify.robertboscacci.com   # public internet
```

`laundry.robertboscacci.com` intentionally remains an A record pointed at the
Optiplex Tailscale IPv4 address:

```text
100.124.5.39
```

`gotify.robertboscacci.com` is public. Route53 aliases it to CloudFront, and
CloudFront forwards to the public Tailscale Funnel origin:

```text
https://optiplex.tailbea63b.ts.net:10000/gotify
```

CloudFront uses the managed `CachingDisabled` cache policy and
`AllViewerExceptHostHeader` origin request policy so Gotify API calls, cookies,
and query strings pass through while the origin TLS handshake still uses the
Tailscale Funnel hostname.

## State

OpenTofu state is stored in S3:

```text
s3://laundry-done-opentofu-state-062008221187/route53-cloudfront/tofu.tfstate
```

State locking uses the shared DynamoDB lock table:

```text
iac-dev-box-tf-locks-062008221187-us-west-2
```

## Apply

Authenticate to AWS with permission to manage the public Route53 hosted zone,
ACM certificates in `us-east-1`, and CloudFront, then run:

```bash
cd infra/route53-cloudfront
tofu init
tofu plan
tofu apply
```

## Runtime

On the Optiplex, Gotify must be served through Tailscale Funnel:

```bash
docker compose up -d --build gotify
tailscale funnel --bg --https=10000 --set-path=/gotify --yes 127.0.0.1:8089
tailscale funnel status
```

Do not use Funnel or Serve on HTTPS `443` for Gotify. That port is reserved for
the Optiplex front-door nginx listener that serves tailnet custom domains such
as `laundry.robertboscacci.com`. If Tailscale owns `443`, Firefox and other
tailnet clients can hit a TLS alert before the request reaches nginx/Caddy.

The `10000/gotify` Funnel origin is public by design for this service. Gotify
authentication and app tokens remain the application-level access control.
