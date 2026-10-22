#!/usr/bin/env bash
# Build once from a clean reviewed commit, publish by identity, deploy by digest.
set -euo pipefail
cd "$(dirname "$0")/.."
[[ -z "$(git status --porcelain)" ]] || { echo 'Clean commit required' >&2; exit 1; }
commit="$(git rev-parse HEAD)"
registry="${LAUNDRY_REGISTRY:?Set approved ECR repository URL}"
[[ "$registry" =~ ^[0-9]+\.dkr\.ecr\.us-west-2\.amazonaws\.com/laundry-done$ ]] || exit 1
free_kb="$(df --output=avail . | tail -1 | tr -d ' ')"
((free_kb >= 30*1024*1024)) || { echo 'At least 30 GiB free required' >&2; exit 1; }
# Dedicated builder makes cleanup safe on the shared Docker host.
builder="laundry-release-${commit:0:12}"
cleanup() { docker buildx rm "$builder" >/dev/null 2>&1 || true; }
trap cleanup EXIT
scripts/validate.sh
[[ "$(git rev-parse HEAD)" == "$commit" && -z "$(git status --porcelain)" ]] || { echo "Source changed during validation" >&2; exit 1; }
docker buildx create --name "$builder" --driver docker-container >/dev/null
aws ecr get-login-password --region us-west-2 | docker login --username AWS --password-stdin "${registry%%/*}" >/dev/null
mkdir -p outputs/releases
if ! aws ecr describe-images --repository-name laundry-done --image-ids "imageTag=git-$commit" --region us-west-2 >/dev/null 2>&1; then
  git archive "$commit:server" | docker buildx build --builder "$builder" --file Dockerfile --tag "$registry:git-$commit" --label "org.opencontainers.image.revision=$commit" --push -
fi
digest="$(aws ecr describe-images --repository-name laundry-done --image-ids "imageTag=git-$commit" --region us-west-2 --query 'imageDetails[0].imageDigest' --output text)"
[[ "$digest" =~ ^sha256:[a-f0-9]{64}$ ]] || exit 1
export RELAY_IMAGE="$registry@$digest"
# No production credentials or production data are needed for simulated dev.
mkdir -p data/dev
if [[ ! -f outputs/dev.env ]]; then
  umask 077
  printf 'DEVICE_SECRET=%s\nMONITOR_TAILSCALE_IPS=127.0.0.1\n' "$(openssl rand -hex 32)" > outputs/dev.env
fi
if [[ "$(stat -c %u data/dev)" != 10001 ]]; then
  docker run --rm --user 0 --entrypoint chown --volume "$PWD/data/dev:/data" "$RELAY_IMAGE" 10001:10001 /data
fi
docker compose -f compose.dev.yaml up -d --wait
set -a
source outputs/dev.env
set +a
SMOKE_URL=http://127.0.0.1:18088 SMOKE_ADMIN_SECRET="$DEVICE_SECRET" node tests/browser/smoke.cjs
unset DEVICE_SECRET
printf '{"commit":"%s","image":"%s","environment":"dev","smoke":"passed"}\n' "$commit" "$RELAY_IMAGE" > "outputs/releases/$commit.json"
df -h .
