#!/usr/bin/env bash
# Exactly one recorded human approval follows manual dev smoke testing.
set -euo pipefail
cd "$(dirname "$0")/.."
tag="${1:?Usage: promote.sh vMAJOR.MINOR.PATCH approval-record.json}"
approval="${2:?Approval record required}"
[[ "$tag" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]] || exit 1
[[ -z "$(git status --porcelain)" ]] || { echo 'Clean checkout required' >&2; exit 1; }
commit="$(git rev-parse "$tag^{commit}")"
[[ "$(git cat-file -t "$tag")" == tag ]] || { echo 'Annotated immutable tag required' >&2; exit 1; }
export RELEASE_TAG="$tag" RELEASE_COMMIT="$commit" APPROVAL_RECORD="$approval"
# Approval record must identify the manually tested dev artifact and human approval.
export RELAY_IMAGE
RELAY_IMAGE="$(conda run --no-capture-output -n "${LAUNDRY_CONDA_ENV:-dell}" python -c 'import json,os;from pathlib import Path;c=os.environ["RELEASE_COMMIT"];d=json.loads(Path("outputs/releases",c+".json").read_text());a=json.loads(Path(os.environ["APPROVAL_RECORD"]).read_text());assert a["tag"]==os.environ["RELEASE_TAG"] and a["image"]==d["image"] and a["environment"]=="production" and a["manual_dev_smoke_passed"] is True and a["approved_by"] and a["approved_at"] and a["approval_url"];assert d["smoke"]=="passed";print(d["image"])')"
[[ "$(git rev-parse HEAD)" == "$commit" ]] || { echo 'Checkout exact tagged commit first' >&2; exit 1; }
# No build: promotion always reuses the dev-tested digest.
docker compose -f compose.yaml -f compose.release.yaml up -d --no-build --wait relay voice-worker
curl --fail --silent http://127.0.0.1:8088/healthz >/dev/null
printf 'Promoted %s at %s\n' "$tag" "$commit"
