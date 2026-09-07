# Security TODO

## Public Gotify Hardening

- [ ] Rotate the Gotify admin username/password to strong unique credentials.
- [ ] Rotate Gotify app tokens after the password change.
- [ ] Confirm the mobile app is using `https://gotify.robertboscacci.com`.
- [ ] Decide whether the public root Funnel at `https://optiplex.tailbea63b.ts.net:10000/` should remain online. It currently serves Elliott Bay VHF and is not required for laundry/Gotify.
- [ ] Add an origin gate before Gotify, such as a local reverse proxy requiring a secret CloudFront header, so direct requests to `https://optiplex.tailbea63b.ts.net:10000/gotify` cannot bypass CloudFront.
- [ ] Consider CloudFront WAF/rate limiting once the origin gate is in place.
