#!/usr/bin/env bash
set -euo pipefail

readonly cut_point="${1:?usage: run_cutpoint.sh CUT_POINT REFERENCE_TRACE RUN_DIR MATRIX_ROW [EVIDENCE_JSON]}"
readonly reference_trace="${2:?usage: run_cutpoint.sh CUT_POINT REFERENCE_TRACE RUN_DIR MATRIX_ROW [EVIDENCE_JSON]}"
readonly run_dir="${3:?usage: run_cutpoint.sh CUT_POINT REFERENCE_TRACE RUN_DIR MATRIX_ROW [EVIDENCE_JSON]}"
readonly matrix_row="${4:?usage: run_cutpoint.sh CUT_POINT REFERENCE_TRACE RUN_DIR MATRIX_ROW [EVIDENCE_JSON]}"
readonly evidence_json="${5:-$run_dir/evidence.json}"
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
readonly final_version="$(grep -a '^FIRMWARE_VERSION=' "$run_dir/uart.log" \
    | tail -n 1 | cut -d= -f2 | tr -d '\r')"
case "$final_version" in
    1.0.0) final_image=v1; durable_state=any ;;
    2.0.0) final_image=v2; durable_state=present ;;
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
    --durable-state "$durable_state" --output "$run_dir/verification.json"

test -s "$run_dir/fault-operation.txt"
test "$(stat -c %s "$run_dir/fault-committed-flash.bin")" -eq 1048576
python3 "$app_root/tests/check_fault_snapshot.py" \
    --evidence "$run_dir/fault-operation.txt" \
    --snapshot "$run_dir/fault-committed-flash.bin" \
    --expected-operation "$cut_point" \
    --output "$run_dir/commit-verification.json"
python3 "$app_root/tests/summarize_cutpoint.py" \
    --cut "$cut_point" --operation-type "$operation_type" \
    --address "$address" --length "$length" --final-image "$final_image" \
    --boots "$boots" --verification "$run_dir/verification.json" \
    --commit-verification "$run_dir/commit-verification.json" \
    --row-output "$matrix_row" --output "$evidence_json" --compact
test -s "$matrix_row"
test -s "$evidence_json"
