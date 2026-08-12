#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=common.sh
source "$SCRIPT_DIR/common.sh"

usage() {
    cat <<'EOF'
usage: setup-x86-worker.sh --config FILE [--install-control-bundle]
                            [--load-runtime] [--build-runtime]
                            [--checkpoints] [--qualify] [--benchmark]

The script never installs or upgrades host packages. Missing dependencies are
reported so the administrator can install pinned packages deliberately.
EOF
}

config= load_runtime=0 build_runtime=0 checkpoints=0 qualify=0 benchmark=0 install_control=0
while (($#)); do
    case "$1" in
        --config) config=${2:?}; shift 2 ;;
        --load-runtime) load_runtime=1; shift ;;
        --install-control-bundle) install_control=1; shift ;;
        --build-runtime) build_runtime=1; shift ;;
        --checkpoints) checkpoints=1; shift ;;
        --qualify) qualify=1; shift ;;
        --benchmark) benchmark=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) proof_die "unknown argument: $1" ;;
    esac
done
[[ -n "$config" ]] || { usage >&2; exit 2; }
proof_load_config "$config"

[[ "$(uname -m)" == x86_64 ]] || proof_die 'x86 worker requires x86_64'
proof_require_command python3
proof_require_command ssh
proof_require_command rsync
proof_require_command sha256sum
proof_check_resources \
    "${PROOF_MIN_CPUS:-4}" "${PROOF_MIN_RAM_BYTES:-4294967296}" \
    "${PROOF_MIN_DISK_BYTES:-53687091200}"
mkdir -p "$PROOF_ROOT"
chmod 0700 "$PROOF_ROOT"
if ((install_control)); then
    proof_install_control_bundle
fi
proof_init_layout
proof_verify_control_bundle
proof_verify_worker_identity

if ((load_runtime)); then
    proof_load_runtime_archive
fi
if ((build_runtime)); then
    [[ ${PROOF_CONTAINER_RUNTIME:-docker} == docker ]] \
        || proof_die 'the checked-in x86 Dockerfile currently requires Docker'
    : "${PROOF_RUNTIME_IMAGE:?PROOF_RUNTIME_IMAGE is required}"
    docker build --pull=false --network host \
        --tag "$PROOF_RUNTIME_IMAGE" "$PROOF_REPO"
fi
proof_verify_runtime_image
if ((checkpoints)); then
    program=${PROOF_CHECKPOINT_PROGRAM:?PROOF_CHECKPOINT_PROGRAM is required}
    "$program" --config "$config"
fi
proof_verify_worker_checkpoints

if [[ -n ${PROOF_PI_HOST:-} ]]; then
    : "${PROOF_PI_USER:?PROOF_PI_USER is required when PROOF_PI_HOST is set}"
    : "${PROOF_SSH_KEY:?PROOF_SSH_KEY is required when PROOF_PI_HOST is set}"
    [[ -r "$PROOF_SSH_KEY" ]] || proof_die "SSH key is missing: $PROOF_SSH_KEY"
    ssh -o BatchMode=yes -o StrictHostKeyChecking=yes \
        -i "$PROOF_SSH_KEY" "$PROOF_PI_USER@$PROOF_PI_HOST" true
fi

if ((qualify)); then
    "$SCRIPT_DIR/qualify-worker.sh" --config "$config"
fi
if ((benchmark)); then
    "$SCRIPT_DIR/benchmark-worker.sh" --config "$config" \
        --worker-counts 1,2,3,4
fi

printf 'x86 worker prepared: root=%s worker=%s\n' "$PROOF_ROOT" "$PROOF_WORKER_ID"
