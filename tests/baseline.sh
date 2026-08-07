#!/usr/bin/env bash
set -euo pipefail

readonly app_root="${APP_ROOT:-/workspace/app}"
readonly output_root="${app_root}/artifacts/baseline"
readonly fixtures="${app_root}/fixtures"

mkdir -p "$output_root"
python3 "$app_root/controller/ota_controller.py" initialize-baseline \
    --output-dir "$output_root"
test "$(stat -c %s "$output_root/baseline-flash.bin")" -eq 1048576
python3 "$app_root/controller/ota_controller.py" baseline-proof \
    --output-dir "$output_root"
python3 "$app_root/controller/ota_controller.py" fault-hook-proof \
    --output-dir "$output_root/fault-hook"

python3 "$app_root/tests/verify_state.py" verify-run \
    --flash "$output_root/confirm/final-flash.bin" \
    --uart-log "$output_root/confirm/uart.log" \
    --mcumgr-list "$output_root/confirm/mcumgr-image-list.txt" \
    --trace "$output_root/confirm/flash-operations.log" \
    --v1-image "$fixtures/sealed-v1-signed.bin" \
    --v2-image "$fixtures/v2-signed.bin" \
    --expected-final v2 --durable-state present \
    --output "$output_root/confirm/verification.json"

python3 "$app_root/tests/verify_state.py" verify-run \
    --flash "$output_root/revert/final-flash.bin" \
    --uart-log "$output_root/revert/uart.log" \
    --mcumgr-list "$output_root/revert/mcumgr-image-list.txt" \
    --trace "$output_root/revert/flash-operations.log" \
    --v1-image "$fixtures/sealed-v1-signed.bin" \
    --v2-image "$fixtures/v2-signed.bin" \
    --expected-final v1 --durable-state any \
    --output "$output_root/revert/verification.json"

readonly hook_cut="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["selected_operation"])' "$output_root/fault-hook/fault-hook-summary.json")"
python3 "$app_root/tests/check_fault_snapshot.py" \
    --evidence "$output_root/fault-hook/injected-cut/fault-operation.txt" \
    --snapshot "$output_root/fault-hook/injected-cut/fault-committed-flash.bin" \
    --expected-operation "$hook_cut" \
    --output "$output_root/fault-hook/commit-verification.json"

python3 - "$output_root" <<'PY'
import hashlib
import json
import sys
from pathlib import Path

root = Path(sys.argv[1])
payload = {
    "result": "pass",
    "confirmed_v2_persistent_boots": 3,
    "revert_final_image": "v1",
    "baseline_flash_sha256": hashlib.sha256(
        (root / "initialization" / "final-flash.bin").read_bytes()).hexdigest(),
}
(root / "baseline-summary.json").write_text(
    json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
PY
