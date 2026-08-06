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
python3 "$app_root/tests/verify_state.py" compare-matrix \
    "${compare_args[@]}" --hash-column flash_hash --hash-column trace_hash \
    --hash-column uart_hash --output "$artifacts/determinism-summary.json"
