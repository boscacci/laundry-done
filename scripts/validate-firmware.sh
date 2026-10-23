#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
free_kb="$(df --output=avail . | tail -1 | tr -d ' ')"
((free_kb >= 30*1024*1024)) || exit 1
builder="laundry-firmware-check-$$"
cleanup() { docker buildx rm "$builder" >/dev/null 2>&1 || true; }
trap cleanup EXIT
mkdir -p outputs/firmware-validation
docker buildx create --name "$builder" --driver docker-container >/dev/null
docker buildx build --builder "$builder" -f Dockerfile.firmware --secret id=laundry_config,src=firmware/include/laundry_config.h.example --build-arg CONFIG_DIGEST=example-config --output type=local,dest=outputs/firmware-validation .
test -s outputs/firmware-validation/firmware.bin
sha256sum outputs/firmware-validation/firmware.bin
