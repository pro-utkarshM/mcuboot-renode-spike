#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=common.sh
source "$SCRIPT_DIR/common.sh"

usage() {
    cat <<'EOF'
usage: setup-pi-worker.sh --config FILE --install-control-bundle --load-runtime
                          [--checkpoints]
                          [--qualify] [--benchmark]

Firmware is never rebuilt by this script. It consumes a frozen, hash-verified
control/input bundle and an ARM64 runtime archive.
EOF
}

config= load_runtime=0 checkpoints=0 qualify=0 benchmark=0 install_control=0
while (($#)); do
    case "$1" in
        --config) config=${2:?}; shift 2 ;;
        --load-runtime) load_runtime=1; shift ;;
        --install-control-bundle) install_control=1; shift ;;
        --checkpoints) checkpoints=1; shift ;;
        --qualify) qualify=1; shift ;;
        --benchmark) benchmark=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) proof_die "unknown argument: $1" ;;
    esac
done
[[ -n "$config" ]] || { usage >&2; exit 2; }
proof_load_config "$config"

[[ "$(uname -m)" == aarch64 ]] || proof_die 'Pi worker requires aarch64'
model=$(tr -d '\0' </proc/device-tree/model 2>/dev/null || true)
[[ "$model" == *'Raspberry Pi 5'* ]] \
    || proof_die "unsupported ARM worker model: ${model:-unknown}"
proof_require_command python3
proof_require_command ip
proof_require_command ssh
proof_require_command rsync
proof_require_command sha256sum
proof_check_resources \
    "${PROOF_MIN_CPUS:-4}" "${PROOF_MIN_RAM_BYTES:-7000000000}" \
    "${PROOF_MIN_DISK_BYTES:-53687091200}"
proof_temperature_report
mkdir -p "$PROOF_ROOT"
chmod 0700 "$PROOF_ROOT"
if ((install_control)); then
    proof_install_control_bundle
fi
proof_init_layout
proof_verify_control_bundle
proof_verify_worker_identity

((load_runtime)) || proof_die 'Pi setup requires --load-runtime with a frozen ARM64 archive'
proof_load_runtime_archive
proof_verify_runtime_image

if [[ ${PROOF_REQUIRE_NO_DEFAULT_ROUTE:-1} == 1 ]] \
        && ip -4 route show default | grep -q .; then
    proof_die 'Pi has an IPv4 default route; remove it for normal isolated proof operation'
fi

if ((checkpoints)); then
    program=${PROOF_CHECKPOINT_PROGRAM:?PROOF_CHECKPOINT_PROGRAM is required}
    "$program" --config "$config"
fi
proof_verify_worker_checkpoints
if ((qualify)); then
    "$SCRIPT_DIR/qualify-worker.sh" --config "$config"
fi
if ((benchmark)); then
    "$SCRIPT_DIR/benchmark-worker.sh" --config "$config" \
        --worker-counts "${PROOF_PI_WORKER_COUNTS:-1,2,3,4}"
fi

qualification=${PROOF_QUALIFICATION_RESULT:-"$PROOF_ROOT/qualification/worker.json"}
if [[ -s "$qualification" ]]; then
    proof_python "$PROOF_REPO/tests/distributed/deploy.py" validate-qualification \
        --manifest "$PROOF_MANIFEST" --worker-id "$PROOF_WORKER_ID" \
        --qualification "$qualification"
else
    printf '%s\n' 'Pi environment prepared for qualification; real proof work remains blocked'
fi
printf 'Pi worker prepared: root=%s worker=%s\n' "$PROOF_ROOT" "$PROOF_WORKER_ID"
