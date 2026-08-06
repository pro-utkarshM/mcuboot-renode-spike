#!/usr/bin/env bash
set -euo pipefail

readonly app_root="${APP_ROOT:-/workspace/app}"
readonly artifacts="${app_root}/artifacts"
readonly repetition="${REPETITION:-standalone}"
readonly trace_dir="${TRACE_DIR:-${artifacts}/cutpoints/${repetition}/clean-trace}"
readonly run_root="${RUN_ROOT:-${artifacts}/cutpoints/${repetition}/runs}"
readonly matrix_csv="${MATRIX_OUTPUT:-${artifacts}/cutpoint-matrix.csv}"
readonly trace_log="${trace_dir}/flash-operations.log"

mkdir -p "$trace_dir" "$run_root" "$(dirname "$matrix_csv")"
python3 "$app_root/controller/ota_controller.py" trace --output-dir "$trace_dir"
python3 "$app_root/tests/verify_state.py" parse-trace \
    --trace "$trace_log" --output "$trace_dir/flash-operations.json"

readonly operation_count="$(grep -c '^op=' "$trace_log")"
test "$operation_count" -gt 0
printf '%s\n' \
    'cut_point,operation,type,address,length,final_image,boots,state_valid,result,flash_hash,trace_hash,uart_hash' \
    > "$matrix_csv"

for cut_point in $(seq 1 "$operation_count"); do
    "$app_root/tests/run_cutpoint.sh" "$cut_point" "$trace_log" \
        "$run_root/$cut_point" "$matrix_csv"
done

if [[ "$matrix_csv" = "${artifacts}/cutpoint-matrix.csv" ]]; then
    cp "$trace_dir/flash-operations.json" "$artifacts/flash-operations.json"
fi
test "$(( $(wc -l < "$matrix_csv") - 1 ))" -eq "$operation_count"
