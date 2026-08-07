#!/usr/bin/env bash
set -euo pipefail

readonly app_root="${APP_ROOT:-/workspace/app}"
readonly artifacts="${app_root}/artifacts/negative-tests"
readonly fixtures="${app_root}/fixtures"
mkdir -p "$artifacts"
python3 "$app_root/tests/verify_fixtures.py" \
    --fixtures "$fixtures" --output "$artifacts/fixture-verification.json"

run_negative() {
    local variant="$1"
    local fixture="$2"
    local output="$artifacts/$variant"
    mkdir -p "$output"
    python3 "$app_root/controller/ota_controller.py" negative-cutpoint \
        --cut 1 --variant "$variant" --output-dir "$output"

    set +e
    python3 "$app_root/tests/verify_state.py" verify-run \
        --flash "$output/final-flash.bin" --uart-log "$output/uart.log" \
        --mcumgr-list "$output/mcumgr-image-list.txt" \
        --trace "$output/flash-operations.log" \
        --v1-image "$fixtures/sealed-v1-signed.bin" \
        --v2-image "$fixtures/$fixture" --expected-final v2 \
        --fault-operation 1 --durable-state present \
        --output "$output/unexpected-pass.json" \
        >"$output/verifier.stdout" 2>"$output/verifier.stderr"
    rc=$?
    set -e
    test "$rc" -eq 2
    test -s "$output/verifier.stderr"
}

run_negative premature-confirm v2-negative-premature-confirm-signed.bin
run_negative erase-after-confirm v2-negative-erase-after-confirm-signed.bin

python3 - "$artifacts" <<'PY'
import json
import sys
from pathlib import Path
root = Path(sys.argv[1])
payload = {
    "result": "pass",
    "sensitivity_result": "both intentionally incorrect variants rejected",
    "variants": [
        {"name": "premature-confirm", "exposed_at_cut_point": 1,
         "bug": "image confirmed while required durable state is skipped"},
        {"name": "erase-after-confirm", "exposed_at_cut_point": 1,
         "bug": "required durable state deleted after image confirmation"},
    ],
}
(root / "negative-tests-summary.json").write_text(
    json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
PY
