#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export LOCAL_UID="$(id -u)"
export LOCAL_GID="$(id -g)"
docker_config="$(mktemp -d "${TMPDIR:-/tmp}/laundry-docker.XXXXXX")"
trap 'rm -rf "${docker_config}"' EXIT
printf '{}\n' >"${docker_config}/config.json"

DOCKER_CONFIG="${docker_config}" docker compose \
  --file "${repo_root}/compose.ota.yaml" \
  run --build --rm ota \
  --config /run/secrets/laundry_config \
  "$@"
