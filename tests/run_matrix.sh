#!/usr/bin/env bash
set -euo pipefail

readonly app_root="${APP_ROOT:-/workspace/app}"
readonly artifacts="${app_root}/artifacts"
readonly repetition="${REPETITION:-standalone}"
readonly trace_dir="${TRACE_DIR:-${artifacts}/cutpoints/${repetition}/clean-trace}"
readonly run_root="${RUN_ROOT:-${artifacts}/cutpoints/${repetition}/runs}"
readonly matrix_csv="${MATRIX_OUTPUT:-${artifacts}/cutpoint-matrix.csv}"
readonly evidence_jsonl="${EVIDENCE_OUTPUT:-$(dirname "$matrix_csv")/cutpoint-evidence.jsonl}"
readonly matrix_lock="${matrix_csv}.lock"
readonly trace_log="${trace_dir}/flash-operations.log"
readonly input_manifest="${trace_dir}/matrix-inputs.json"
readonly matrix_summary="${matrix_csv%.csv}-summary.json"
readonly jobs="${MATRIX_JOBS:-8}"
readonly batch_limit="${MATRIX_BATCH_LIMIT:-0}"

if [[ ! "$jobs" =~ ^[1-9][0-9]*$ ]]; then
    echo "MATRIX_JOBS must be a positive integer" >&2
    exit 2
fi
if [[ ! "$batch_limit" =~ ^[0-9]+$ ]]; then
    echo "MATRIX_BATCH_LIMIT must be a non-negative integer" >&2
    exit 2
fi
command -v flock >/dev/null || {
    echo "flock is required for matrix checkpoint locking" >&2
    exit 2
}

mkdir -p "$trace_dir" "$run_root" "$(dirname "$matrix_csv")"

exec 9>"$matrix_lock"
if ! flock -n 9; then
    echo "matrix checkpoint is already locked: $matrix_lock" >&2
    exit 2
fi

if [[ -f "$matrix_csv" && ! -f "$trace_log" ]]; then
    echo "cannot resume matrix without its clean reference trace: $trace_log" >&2
    exit 2
fi

if [[ ! -f "$trace_log" ]]; then
    python3 "$app_root/controller/ota_controller.py" trace --output-dir "$trace_dir"
fi
python3 "$app_root/tests/verify_state.py" parse-trace \
    --trace "$trace_log" --output "$trace_dir/flash-operations.json"

readonly operation_count="$(grep -c '^op=' "$trace_log")"
test "$operation_count" -gt 0

python3 - "$artifacts/baseline/baseline-flash.bin" \
    "$app_root/fixtures/v2-auto-confirm-signed.bin" "$trace_log" \
    "$input_manifest" "$operation_count" <<'PY'
import hashlib
import json
import sys
from pathlib import Path

baseline, image, trace, manifest = map(Path, sys.argv[1:5])
payload = {
    "baseline_flash_sha256": hashlib.sha256(baseline.read_bytes()).hexdigest(),
    "signed_v2_sha256": hashlib.sha256(image.read_bytes()).hexdigest(),
    "clean_trace_sha256": hashlib.sha256(trace.read_bytes()).hexdigest(),
    "operation_count": int(sys.argv[5]),
}
if manifest.exists():
    recorded = json.loads(manifest.read_text(encoding="utf-8"))
    if recorded != payload:
        raise SystemExit(
            "matrix inputs or clean trace changed; refusing an unsafe resume")
else:
    manifest.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8")
PY

readonly header="$(python3 "$app_root/tests/summarize_cutpoint.py" --header)"
if [[ ! -f "$matrix_csv" ]]; then
    printf '%s\n' "$header" > "$matrix_csv"
    : > "$evidence_jsonl"
else
    test "$(head -n 1 "$matrix_csv")" = "$header"
    test -f "$evidence_jsonl"
fi

completed="$(( $(wc -l < "$matrix_csv") - 1 ))"
test "$completed" -ge 0
test "$(wc -l < "$evidence_jsonl")" -eq "$completed"
if [[ "$completed" -gt 0 ]]; then
    python3 "$app_root/tests/verify_state.py" validate-matrix \
        --matrix "$matrix_csv" --expected-cut-points "$operation_count" \
        --output "$matrix_summary"
fi

if [[ "$completed" -lt "$operation_count" ]]; then
    scratch="$(mktemp -d "/tmp/ota-matrix-${repetition}.XXXXXX")"
    trap 'rm -rf "$scratch"' EXIT

    next_cut="$((completed + 1))"
    batches_run=0
    while [[ "$next_cut" -le "$operation_count" ]]; do
        batch_end="$((next_cut + jobs - 1))"
        if [[ "$batch_end" -gt "$operation_count" ]]; then
            batch_end="$operation_count"
        fi

        pids=()
        cuts=()
        for cut_point in $(seq "$next_cut" "$batch_end"); do
            cut_dir="$scratch/$cut_point"
            mkdir -p "$cut_dir"
            "$app_root/tests/run_cutpoint.sh" "$cut_point" "$trace_log" \
                "$cut_dir" "$cut_dir/row.csv" "$cut_dir/evidence.json" &
            pids+=("$!")
            cuts+=("$cut_point")
        done

        batch_status=0
        for pid in "${pids[@]}"; do
            if ! wait "$pid"; then
                batch_status=1
            fi
        done
        if [[ "$batch_status" -ne 0 ]]; then
            echo "matrix batch ${next_cut}-${batch_end} failed; no rows committed" >&2
            exit 2
        fi

        for cut_point in "${cuts[@]}"; do
            cut_dir="$scratch/$cut_point"
            test "$(wc -l < "$cut_dir/row.csv")" -eq 1
            test "$(wc -l < "$cut_dir/evidence.json")" -eq 1
            test "$(cut -d, -f1 "$cut_dir/row.csv")" -eq "$cut_point"
            cat "$cut_dir/row.csv" >> "$matrix_csv"
            cat "$cut_dir/evidence.json" >> "$evidence_jsonl"
            rm -rf "$cut_dir"
        done
        completed="$batch_end"
        next_cut="$((completed + 1))"
        batches_run="$((batches_run + 1))"
        printf 'matrix progress: %s/%s\n' "$completed" "$operation_count"
        if [[ "$batch_limit" -gt 0 && "$batches_run" -ge "$batch_limit" \
            && "$completed" -lt "$operation_count" ]]; then
            echo "matrix checkpoint saved; full matrix remains incomplete" >&2
            exit 75
        fi
    done
fi

python3 "$app_root/tests/verify_state.py" validate-matrix \
    --matrix "$matrix_csv" --expected-cut-points "$operation_count" \
    --complete --output "$matrix_summary"
test "$(wc -l < "$evidence_jsonl")" -eq "$operation_count"

if [[ "$matrix_csv" = "${artifacts}/cutpoint-matrix.csv" ]]; then
    cp "$trace_dir/flash-operations.json" "$artifacts/flash-operations.json"
fi
