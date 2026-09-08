#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export LOCAL_UID="$(id -u)"
export LOCAL_GID="$(id -g)"

exec docker compose \
  --file "${repo_root}/compose.ota.yaml" \
  run --build --rm ota \
  --config /run/secrets/laundry_config \
  "$@"
