#!/usr/bin/env bash
# The executable OTA staging path lives in ota_controller.py::stage_update and
# invokes Apache mcumgr over /tmp/mcumgr-uart. This wrapper is intentionally
# narrow so CI has an obvious, auditable entry point.
set -euo pipefail

readonly output_dir="${1:?usage: stage_update.sh OUTPUT_DIR [IMAGE]}"
readonly image="${2:-/workspace/app/fixtures/v2-auto-confirm-signed.bin}"
exec python3 /workspace/app/controller/ota_controller.py trace \
    --output-dir "$output_dir" --image "$image"
