#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
free_kb="$(df --output=avail . | tail -1 | tr -d ' ')"
((free_kb >= 30*1024*1024)) || { echo 'At least 30 GiB free required' >&2; exit 1; }
builder="laundry-tests-$$"
tag="laundry-done-task-tests:run-$$"
cleanup() { docker image rm "$tag" >/dev/null 2>&1 || true; docker buildx rm "$builder" >/dev/null 2>&1 || true; }
trap cleanup EXIT
df -h .
docker system df
docker buildx create --name "$builder" --driver docker-container >/dev/null
docker buildx build --builder "$builder" --load -f Dockerfile.test -t "$tag" .
docker run --rm --network none --cap-drop ALL --security-opt no-new-privileges:true "$tag"
df -h .
