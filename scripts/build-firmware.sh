#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
config_path="${repo_root}/firmware/include/laundry_config.h"
output_path="${repo_root}/outputs"

if [[ ! -f "${config_path}" ]]; then
  echo "Missing ${config_path}; copy and edit laundry_config.h.example first." >&2
  exit 1
fi

config_digest="$(sha256sum "${config_path}" | awk '{print $1}')"
mkdir -p "${output_path}"
docker_config="$(mktemp -d "${TMPDIR:-/tmp}/laundry-docker.XXXXXX")"
staging_path="$(mktemp -d "${output_path}/.firmware-build.XXXXXX")"
trap 'rm -rf "${docker_config}" "${staging_path}"' EXIT
printf '{}\n' >"${docker_config}/config.json"

DOCKER_CONFIG="${docker_config}" docker build \
  --file "${repo_root}/Dockerfile.firmware" \
  --secret "id=laundry_config,src=${config_path}" \
  --build-arg "CONFIG_DIGEST=${config_digest}" \
  --output "type=local,dest=${staging_path}" \
  "${repo_root}"

chmod 600 "${staging_path}/firmware.bin"
mv -f "${staging_path}/firmware.bin" "${output_path}/firmware.bin"
sha256sum "${output_path}/firmware.bin"
