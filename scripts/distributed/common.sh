#!/usr/bin/env bash
set -euo pipefail

proof_die() {
    printf 'error: %s\n' "$*" >&2
    exit 2
}

proof_require_command() {
    command -v "$1" >/dev/null 2>&1 || proof_die "required command not found: $1"
}

proof_load_config() {
    local config=${1:?configuration path is required}
    [[ -r "$config" ]] || proof_die "cannot read configuration: $config"
    # This is an operator-owned shell environment file, not untrusted proof data.
    # shellcheck disable=SC1090
    source "$config"
    : "${PROOF_ROOT:?PROOF_ROOT is required}"
    : "${PROOF_REPO:?PROOF_REPO is required}"
    : "${PROOF_MANIFEST:?PROOF_MANIFEST is required}"
    : "${PROOF_WORKER_ID:?PROOF_WORKER_ID is required}"
    : "${PROOF_EXECUTOR_ID:?PROOF_EXECUTOR_ID is required}"
    : "${PROOF_RUNTIME_IMAGE_ID:?PROOF_RUNTIME_IMAGE_ID is required}"
    PROOF_LINEAGES_DIR=${PROOF_LINEAGES_DIR:-"$PROOF_ROOT/control/lineages"}
    PROOF_SHARDS_ALL=${PROOF_SHARDS_ALL:-"$PROOF_ROOT/shards/all"}
    PROOF_SHARDS_READY=${PROOF_SHARDS_READY:-"$PROOF_ROOT/shards/ready"}
    PROOF_RESULTS=${PROOF_RESULTS:-"$PROOF_ROOT/results"}
    PROOF_JOURNALS=${PROOF_JOURNALS:-"$PROOF_ROOT/journals"}
    PROOF_FAILURES=${PROOF_FAILURES:-"$PROOF_ROOT/failures"}
    PROOF_LOCKS=${PROOF_LOCKS:-"$PROOF_ROOT/state/locks"}
    PROOF_WORKER_JOBS=${PROOF_WORKER_JOBS:-1}
    export PROOF_ROOT PROOF_REPO PROOF_MANIFEST PROOF_WORKER_ID
    export PROOF_EXECUTOR_ID PROOF_RUNTIME_IMAGE_ID
    export PROOF_LINEAGES_DIR PROOF_SHARDS_ALL PROOF_SHARDS_READY
    export PROOF_RESULTS PROOF_JOURNALS PROOF_FAILURES PROOF_LOCKS
    export PROOF_WORKER_JOBS
}

proof_python() {
    PYTHONPATH="$PROOF_REPO${PYTHONPATH:+:$PYTHONPATH}" \
        PYTHONDONTWRITEBYTECODE=1 python3 "$@"
}

proof_init_layout() {
    proof_python "$PROOF_REPO/tests/distributed/deploy.py" \
        init-layout --root "$PROOF_ROOT"
}

proof_install_control_bundle() {
    local archive digest actual staging destination
    archive=${PROOF_CONTROL_BUNDLE:?PROOF_CONTROL_BUNDLE is required}
    digest=${PROOF_CONTROL_BUNDLE_SHA256:?PROOF_CONTROL_BUNDLE_SHA256 is required}
    [[ -r "$archive" ]] || proof_die "control bundle is missing: $archive"
    actual=$(sha256sum "$archive" | awk '{print $1}')
    [[ "$actual" == "$digest" ]] \
        || proof_die "control bundle hash mismatch: $actual != $digest"
    if tar -tf "$archive" | grep -Eq '(^/|(^|/)\.\.(/|$))'; then
        proof_die 'control bundle contains an unsafe path'
    fi
    mkdir -p "$PROOF_ROOT/state"
    staging=$(mktemp -d "$PROOF_ROOT/state/control.XXXXXX")
    tar -xf "$archive" -C "$staging"
    [[ -z $(find "$staging" -type l -print -quit) ]] \
        || proof_die 'control bundle must not contain symbolic links'
    [[ -f "$staging/manifest.json" && -d "$staging/lineages" ]] \
        || proof_die 'control bundle must contain manifest.json and lineages/'
    destination="$PROOF_ROOT/control"
    if [[ -d "$destination" ]] \
            && [[ -n $(find "$destination" -mindepth 1 -type f -print -quit) ]]; then
        diff -qr "$staging" "$destination" >/dev/null \
            || proof_die 'existing immutable control bundle differs from supplied bundle'
        find "$staging" -type f -delete
        find "$staging" -depth -type d -empty -delete
    else
        rmdir "$destination/lineages" "$destination/profiles" "$destination" \
            2>/dev/null || true
        mv "$staging" "$destination"
        chmod -R a-w "$destination"
    fi
}

proof_check_resources() {
    local minimum_cpus=$1 minimum_ram_bytes=$2 minimum_disk_bytes=$3
    local cpus ram_bytes disk_bytes
    cpus=$(getconf _NPROCESSORS_ONLN)
    ram_bytes=$(awk '/^MemTotal:/ { print $2 * 1024 }' /proc/meminfo)
    disk_bytes=$(df -PB1 "$(dirname "$PROOF_ROOT")" | awk 'NR == 2 { print $4 }')
    (( cpus >= minimum_cpus )) || proof_die "need at least $minimum_cpus CPUs; found $cpus"
    awk -v actual="$ram_bytes" -v minimum="$minimum_ram_bytes" \
        'BEGIN { exit !(actual >= minimum) }' \
        || proof_die "insufficient RAM: $ram_bytes bytes"
    (( disk_bytes >= minimum_disk_bytes )) \
        || proof_die "insufficient free disk: $disk_bytes bytes"
}

proof_verify_control_bundle() {
    proof_python -c \
        'from pathlib import Path; from tests.distributed.manifest import load_manifest,hash_declared_inputs,validate_local_environment; p=Path(__import__("sys").argv[1]); m=load_manifest(p); hash_declared_inputs(m,p.parent); validate_local_environment(m,p.parent); print(m["proof_id"])' \
        "$PROOF_MANIFEST"
}

proof_verify_worker_identity() {
    : "${PROOF_WORKER_ARCH:?PROOF_WORKER_ARCH is required}"
    : "${PROOF_BUILD_IDENTITY:?PROOF_BUILD_IDENTITY is required}"
    proof_python -c \
        'from pathlib import Path; from tests.distributed.manifest import load_manifest; import os,sys; m=load_manifest(Path(sys.argv[1])); w=m["qualified_workers"].get(os.environ["PROOF_WORKER_ID"]); expected={"architecture":os.environ["PROOF_WORKER_ARCH"],"build_identity":os.environ["PROOF_BUILD_IDENTITY"],"executor_id":os.environ["PROOF_EXECUTOR_ID"],"runtime_image_id":os.environ["PROOF_RUNTIME_IMAGE_ID"]}; valid=isinstance(w,dict) and all(w.get(k)==v for k,v in expected.items()); print(os.environ["PROOF_WORKER_ID"] if valid else "worker identity is not frozen by manifest", file=sys.stdout if valid else sys.stderr); raise SystemExit(0 if valid else 2)' \
        "$PROOF_MANIFEST"
}

proof_verify_worker_checkpoints() {
    proof_python "$PROOF_REPO/tests/distributed/deploy.py" verify-checkpoints \
        --manifest "$PROOF_MANIFEST" \
        --lineages-dir "$PROOF_LINEAGES_DIR" \
        --worker-id "$PROOF_WORKER_ID"
}

proof_container_runtime() {
    if [[ -n ${PROOF_CONTAINER_RUNTIME:-} ]]; then
        proof_require_command "$PROOF_CONTAINER_RUNTIME"
        printf '%s\n' "$PROOF_CONTAINER_RUNTIME"
    elif command -v podman >/dev/null 2>&1; then
        printf '%s\n' podman
    elif command -v docker >/dev/null 2>&1; then
        printf '%s\n' docker
    else
        proof_die 'Docker or Podman is required; install one explicitly and rerun'
    fi
}

proof_load_runtime_archive() {
    local runtime archive digest actual
    runtime=$(proof_container_runtime)
    archive=${PROOF_RUNTIME_ARCHIVE:?PROOF_RUNTIME_ARCHIVE is required}
    digest=${PROOF_RUNTIME_ARCHIVE_SHA256:?PROOF_RUNTIME_ARCHIVE_SHA256 is required}
    [[ -r "$archive" ]] || proof_die "runtime archive is missing: $archive"
    actual=$(sha256sum "$archive" | awk '{print $1}')
    [[ "$actual" == "$digest" ]] \
        || proof_die "runtime archive hash mismatch: $actual != $digest"
    "$runtime" load --input "$archive"
}

proof_verify_runtime_image() {
    local runtime image expected_arch actual_arch actual_id
    runtime=$(proof_container_runtime)
    image=${PROOF_RUNTIME_IMAGE:?PROOF_RUNTIME_IMAGE is required}
    expected_arch=${PROOF_RUNTIME_ARCH:?PROOF_RUNTIME_ARCH is required}
    actual_arch=$($runtime image inspect --format '{{.Architecture}}' "$image")
    [[ "$actual_arch" == "$expected_arch" ]] \
        || proof_die "runtime architecture mismatch: $actual_arch != $expected_arch"
    actual_id=$($runtime image inspect --format '{{.Id}}' "$image")
    [[ "$actual_id" == "$PROOF_RUNTIME_IMAGE_ID" ]] \
        || proof_die "runtime image ID mismatch: $actual_id != $PROOF_RUNTIME_IMAGE_ID"
}

proof_temperature_report() {
    if command -v vcgencmd >/dev/null 2>&1; then
        vcgencmd measure_temp
        vcgencmd get_throttled
    elif [[ -r /sys/class/thermal/thermal_zone0/temp ]]; then
        awk '{ printf "temperature_c=%.1f\n", $1 / 1000 }' \
            /sys/class/thermal/thermal_zone0/temp
    else
        printf '%s\n' 'temperature=unavailable'
    fi
}
