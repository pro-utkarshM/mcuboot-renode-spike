#!/usr/bin/env bash
set -euo pipefail

readonly app_root="${APP_ROOT:-/workspace/app}"
readonly artifacts="${app_root}/artifacts"
readonly repetition_root="${artifacts}/repetitions"
mkdir -p "$repetition_root"

matrices=()
first_matrix="${repetition_root}/1/cutpoint-matrix.csv"
mkdir -p "$(dirname "$first_matrix")"
if [[ -f "${artifacts}/cutpoint-matrix.csv" ]]; then
    cp "${artifacts}/cutpoint-matrix.csv" "$first_matrix"
else
    REPETITION=1 \
    TRACE_DIR="${repetition_root}/1/clean-trace" \
    RUN_ROOT="${repetition_root}/1/runs" \
    MATRIX_OUTPUT="$first_matrix" \
        "$app_root/tests/run_matrix.sh"
fi
matrices+=("$first_matrix")

for repetition in 2 3 4 5; do
    matrix="${repetition_root}/${repetition}/cutpoint-matrix.csv"
    matrices+=("$matrix")
    REPETITION="$repetition" \
    TRACE_DIR="${repetition_root}/${repetition}/clean-trace" \
    RUN_ROOT="${repetition_root}/${repetition}/runs" \
    MATRIX_OUTPUT="$matrix" \
        "$app_root/tests/run_matrix.sh"
done

compare_args=()
for matrix in "${matrices[@]}"; do
    compare_args+=(--matrix "$matrix")
done
if [[ -f "${artifacts}/flash-operations.json" ]]; then
    reference_operations="${artifacts}/flash-operations.json"
else
    reference_operations="${repetition_root}/1/clean-trace/flash-operations.json"
fi
readonly expected_cut_points="$(python3 -c 'import json,sys; print(len(json.load(open(sys.argv[1]))["operations"]))' "$reference_operations")"
python3 "$app_root/tests/verify_state.py" compare-matrix \
    "${compare_args[@]}" --expected-cut-points "$expected_cut_points" \
    --hash-column flash_hash --hash-column trace_hash \
    --hash-column uart_semantic_hash --hash-column mcumgr_hash \
    --hash-column fault_snapshot_hash \
    --output "$artifacts/determinism-summary.json"
