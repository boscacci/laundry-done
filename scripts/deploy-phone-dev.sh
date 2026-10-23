#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
[[ -z "$(git status --porcelain)" ]] || { echo 'Clean reviewed commit required' >&2; exit 1; }
commit="$(git rev-parse HEAD)"
scripts/validate.sh
[[ "$(git rev-parse HEAD)" == "$commit" && -z "$(git status --porcelain)" ]] || { echo "Source changed during validation" >&2; exit 1; }
scripts/prepare-phone-identity.sh
export TF_VAR_certificate_bundle
TF_VAR_certificate_bundle="$(cat "${LAUNDRY_IDENTITY_DIR:-$PWD/data/phone-identity}/ca.pem")"
tofu -chdir=infra/phone-alerts init -input=false
tofu -chdir=infra/phone-alerts plan -input=false -out=../../outputs/phone-dev.tfplan
tofu -chdir=infra/phone-alerts apply -input=false -auto-approve ../../outputs/phone-dev.tfplan
bucket="$(tofu -chdir=infra/phone-alerts output -raw audio_bucket)"
conda run --no-capture-output -n "${LAUNDRY_CONDA_ENV:-dell}" python scripts/generate-audio.py --publish-bucket "$bucket"
# Only non-secret outputs. The phone number is intentionally not printed.
tofu -chdir=infra/phone-alerts output -json > outputs/phone-dev-outputs.json
chmod 600 outputs/phone-dev-outputs.json
