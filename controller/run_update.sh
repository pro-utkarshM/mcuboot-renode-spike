#!/usr/bin/env bash
set -euo pipefail

readonly cut_point="${1:?usage: run_update.sh CUT_POINT OUTPUT_DIR [IMAGE]}"
readonly output_dir="${2:?usage: run_update.sh CUT_POINT OUTPUT_DIR [IMAGE]}"
readonly image="${3:-/workspace/app/fixtures/v2-auto-confirm-signed.bin}"
exec python3 /workspace/app/controller/ota_controller.py cutpoint \
    --cut "$cut_point" --output-dir "$output_dir" --image "$image"
