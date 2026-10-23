#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
conda run --no-capture-output -n "${LAUNDRY_CONDA_ENV:-dell}" python -m pytest server/tests -q
if command -v tofu >/dev/null; then tofu -chdir=infra/phone-alerts fmt -check; tofu -chdir=infra/phone-alerts validate; fi
git diff --check
conda run --no-capture-output -n "${LAUNDRY_CONDA_ENV:-dell}" ruff check server/src/laundry_done_relay/delivery.py server/src/laundry_done_relay/voice_worker.py services/phone_alerts scripts/generate-audio.py scripts/configure-phone-worker.py scripts/check-image-security.py server/tests/test_delivery.py server/tests/test_phone_service.py server/tests/test_phone_flow.py server/tests/test_image_security.py
conda run --no-capture-output -n "${LAUNDRY_CONDA_ENV:-dell}" python -m pip_audit -r server/requirements.txt
