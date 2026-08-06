#!/usr/bin/env bash
set -euo pipefail

readonly cut_point="${1:?usage: run_cutpoint.sh CUT_POINT REFERENCE_TRACE RUN_DIR MATRIX_CSV}"
readonly reference_trace="${2:?usage: run_cutpoint.sh CUT_POINT REFERENCE_TRACE RUN_DIR MATRIX_CSV}"
readonly run_dir="${3:?usage: run_cutpoint.sh CUT_POINT REFERENCE_TRACE RUN_DIR MATRIX_CSV}"
readonly matrix_csv="${4:?usage: run_cutpoint.sh CUT_POINT REFERENCE_TRACE RUN_DIR MATRIX_CSV}"
readonly app_root="${APP_ROOT:-/workspace/app}"
readonly fixtures="${app_root}/fixtures"

mkdir -p "$run_dir"
python3 "$app_root/controller/ota_controller.py" cutpoint \
    --cut "$cut_point" --output-dir "$run_dir"

readonly expected_record="$(sed -n "${cut_point}p" "$reference_trace")"
readonly actual_record="$(sed -n "${cut_point}p" "$run_dir/flash-operations.log")"
test -n "$expected_record"
test "$actual_record" = "$expected_record"

read -r op_field type_field address_field length_field <<<"$expected_record"
readonly operation="${op_field#op=}"
readonly operation_type="${type_field#type=}"
readonly address="${address_field#address=}"
readonly length="${length_field#length=}"
readonly final_version="$(grep -a '^FIRMWARE_VERSION=' "$run_dir/uart.log" | tail -n 1 | cut -d= -f2)"
case "$final_version" in
    1.0.0) final_image=v1 ;;
    2.0.0) final_image=v2 ;;
    *) echo "invalid final firmware marker: $final_version" >&2; exit 2 ;;
esac
readonly boots="$(grep -a -c '^FIRMWARE_VERSION=' "$run_dir/uart.log")"

python3 "$app_root/tests/verify_state.py" verify-run \
    --flash "$run_dir/final-flash.bin" \
    --uart-log "$run_dir/uart.log" \
    --mcumgr-list "$run_dir/mcumgr-image-list.txt" \
    --trace "$run_dir/flash-operations.log" \
    --v1-image "$fixtures/sealed-v1-signed.bin" \
    --v2-image "$fixtures/v2-auto-confirm-signed.bin" \
    --expected-final "$final_image" --fault-operation "$cut_point" \
    --durable-state present --output "$run_dir/verification.json"

test -s "$run_dir/fault-operation.txt"
test "$(stat -c %s "$run_dir/fault-committed-flash.bin")" -eq 1048576
python3 "$app_root/tests/check_fault_snapshot.py" \
    --evidence "$run_dir/fault-operation.txt" \
    --snapshot "$run_dir/fault-committed-flash.bin" \
    --expected-operation "$cut_point" \
    --output "$run_dir/commit-verification.json"
readonly flash_hash="$(sha256sum "$run_dir/final-flash.bin" | cut -d' ' -f1)"
readonly trace_hash="$(sha256sum "$run_dir/flash-operations.log" | cut -d' ' -f1)"
readonly uart_hash="$(sha256sum "$run_dir/uart.log" | cut -d' ' -f1)"
printf '%s,%s,%s,%s,%s,%s,%s,true,pass,%s,%s,%s\n' \
    "$cut_point" "$operation" "$operation_type" "$address" "$length" \
    "$final_image" "$boots" "$flash_hash" "$trace_hash" "$uart_hash" \
    >> "$matrix_csv"
