#!/usr/bin/env bash
# Adds only a private tailnet listener; preserves shared 443 and Funnel routes.
set -euo pipefail
cd "$(dirname "$0")/.."
[[ -z "$(git status --porcelain)" ]] || { echo 'Clean reviewed commit required' >&2; exit 1; }
export RELAY_IMAGE="${RELAY_IMAGE:?Set dev-tested digest}"
gateway="$(docker network inspect laundry-done-dev_default --format '{{(index .IPAM.Config 0).Gateway}}')"
[[ "$gateway" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] || exit 1
# These are nonsecret access settings for a loopback-only application listener.
sed -i '/^MONITOR_TAILSCALE_IPS=/d; /^TRUSTED_PROXY_IPS=/d' outputs/dev.env
printf 'MONITOR_TAILSCALE_IPS=100.64.0.0/10\nTRUSTED_PROXY_IPS=%s\n' "$gateway" >> outputs/dev.env
docker compose -f compose.dev.yaml up -d --no-build --wait relay
tailscale serve --bg --https=18445 http://127.0.0.1:18088
