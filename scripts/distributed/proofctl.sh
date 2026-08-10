#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=common.sh
source "$SCRIPT_DIR/common.sh"

usage() {
    cat <<'EOF'
usage: proofctl.sh --config FILE COMMAND [ARGS]

commands:
  init                 initialize the private worker directory layout
  install-local        promote this worker's owned shards into its ready queue
  install-incoming     validate/promote shards received in transfer/incoming
  resume               process pending local shards once using bounded slots
  qualification-resume run only the isolated qualification queue
  worker-slot N        continuously process slot N (for systemd)
  status               show validated PASS and pending case counts
  pending-shards       list shards with unfinished cases
  completed-shards     list shards whose cases all have validated PASS evidence
  push-pi              stage and transfer Pi-owned shards, then promote remotely
  collect-pi           transfer Pi results back and promote centrally
  aggregate            reconstruct matrices and invoke the existing verifier
  central-gates        run baseline/negative/offline gates on the x86 node
  finalize             run verifier self-test and existing finalizer on x86
EOF
}

config=
[[ ${1:-} == --config ]] || { usage >&2; exit 2; }
config=${2:?configuration path is required}
shift 2
command_name=${1:-}
shift || true
proof_load_config "$config"
proof_id=$(proof_python -c \
    'from pathlib import Path; from tests.distributed.manifest import load_manifest; import sys; print(load_manifest(Path(sys.argv[1]))["proof_id"])' \
    "$PROOF_MANIFEST")

deploy=(proof_python "$PROOF_REPO/tests/distributed/deploy.py")
context=(--manifest "$PROOF_MANIFEST" --lineages-dir "$PROOF_LINEAGES_DIR")
qualification_args=()
worker_arch_lower=${PROOF_WORKER_ARCH:-}
worker_arch_lower=${worker_arch_lower,,}
if [[ ${PROOF_REQUIRE_QUALIFICATION:-0} == 1 \
        || "$worker_arch_lower" == *arm64* \
        || "$worker_arch_lower" == *aarch64* ]]; then
    qualification=${PROOF_QUALIFICATION_RESULT:-"$PROOF_ROOT/qualification/worker.json"}
    qualification_args=(--qualification "$qualification")
fi

run_slot() {
    local slot=$1 once_flag=${2:-}
    "${deploy[@]}" worker "${context[@]}" \
        --ready "$PROOF_SHARDS_READY" \
        --results "$PROOF_RESULTS" \
        --journals "$PROOF_JOURNALS" \
        --failures "$PROOF_FAILURES" \
        --locks "$PROOF_LOCKS" \
        --worker-id "$PROOF_WORKER_ID" \
        --slot "$slot" --slots "$PROOF_WORKER_JOBS" \
        "${qualification_args[@]}" $once_flag
}

case "$command_name" in
    init)
        proof_init_layout
        ;;
    install-local)
        "${deploy[@]}" stage-shards "${context[@]}" \
            --source "$PROOF_SHARDS_ALL" --staging "$PROOF_SHARDS_READY" \
            --worker-id "$PROOF_WORKER_ID"
        ;;
    install-incoming)
        "${deploy[@]}" install-shards "${context[@]}" \
            --source "$PROOF_ROOT/transfer/incoming/shards/$proof_id" \
            --ready "$PROOF_SHARDS_READY" --worker-id "$PROOF_WORKER_ID"
        ;;
    resume)
        pids=()
        for ((slot = 0; slot < PROOF_WORKER_JOBS; slot++)); do
            run_slot "$slot" --once &
            pids+=("$!")
        done
        status=0
        for pid in "${pids[@]}"; do
            wait "$pid" || status=2
        done
        exit "$status"
        ;;
    qualification-resume)
        : "${PROOF_QUALIFICATION_READY:?PROOF_QUALIFICATION_READY is required}"
        : "${PROOF_CANDIDATE_RESULTS:?PROOF_CANDIDATE_RESULTS is required}"
        "${deploy[@]}" worker "${context[@]}" \
            --ready "$PROOF_QUALIFICATION_READY" \
            --results "$PROOF_CANDIDATE_RESULTS" \
            --journals "$PROOF_ROOT/qualification/journals" \
            --failures "$PROOF_ROOT/qualification/failures" \
            --locks "$PROOF_ROOT/qualification/locks" \
            --worker-id "$PROOF_WORKER_ID" --slot 0 --slots 1 --once
        ;;
    worker-slot)
        slot=${1:?worker-slot requires a zero-based slot number}
        run_slot "$slot"
        ;;
    status)
        "${deploy[@]}" status "${context[@]}" \
            --shards "$PROOF_SHARDS_READY" --results "$PROOF_RESULTS" \
            --worker-id "$PROOF_WORKER_ID"
        ;;
    pending-shards|completed-shards)
        state=${command_name%-shards}
        "${deploy[@]}" status "${context[@]}" \
            --shards "$PROOF_SHARDS_READY" --results "$PROOF_RESULTS" \
            --worker-id "$PROOF_WORKER_ID" --state "$state"
        ;;
    push-pi)
        : "${PROOF_PI_HOST:?PROOF_PI_HOST is required}"
        : "${PROOF_PI_USER:?PROOF_PI_USER is required}"
        : "${PROOF_PI_WORKER_ID:?PROOF_PI_WORKER_ID is required}"
        : "${PROOF_PI_ROOT:?PROOF_PI_ROOT is required}"
        : "${PROOF_PI_REPO:?PROOF_PI_REPO is required}"
        : "${PROOF_PI_CONFIG:?PROOF_PI_CONFIG is required}"
        : "${PROOF_SSH_KEY:?PROOF_SSH_KEY is required}"
        outgoing="$PROOF_ROOT/transfer/outgoing/pi-shards/$proof_id"
        mkdir -p "$outgoing"
        chmod 0700 "$outgoing"
        "${deploy[@]}" stage-shards "${context[@]}" \
            --source "$PROOF_SHARDS_ALL" --staging "$outgoing" \
            --worker-id "$PROOF_PI_WORKER_ID"
        remote="$PROOF_PI_USER@$PROOF_PI_HOST"
        ssh_options=(-i "$PROOF_SSH_KEY" -o BatchMode=yes -o StrictHostKeyChecking=yes)
        ssh "${ssh_options[@]}" "$remote" \
            install -d -m 0700 "$PROOF_PI_ROOT/transfer/incoming/shards/$proof_id"
        rsync -rlpt --ignore-existing --delay-updates \
            --partial-dir=.rsync-partial --timeout=60 --protect-args \
            -e "ssh -i $PROOF_SSH_KEY -o BatchMode=yes -o StrictHostKeyChecking=yes" \
            "$outgoing/" "$remote:$PROOF_PI_ROOT/transfer/incoming/shards/$proof_id/"
        ssh "${ssh_options[@]}" "$remote" \
            "$PROOF_PI_REPO/scripts/distributed/proofctl.sh" \
            --config "$PROOF_PI_CONFIG" install-incoming
        ;;
    collect-pi)
        : "${PROOF_PI_HOST:?PROOF_PI_HOST is required}"
        : "${PROOF_PI_USER:?PROOF_PI_USER is required}"
        : "${PROOF_PI_ROOT:?PROOF_PI_ROOT is required}"
        : "${PROOF_SSH_KEY:?PROOF_SSH_KEY is required}"
        incoming="$PROOF_ROOT/transfer/incoming/results/pi/$proof_id"
        mkdir -p "$incoming"
        chmod 0700 "$incoming"
        remote="$PROOF_PI_USER@$PROOF_PI_HOST"
        rsync -rlpt --ignore-existing --delay-updates \
            --partial-dir=.rsync-partial --timeout=60 --protect-args \
            -e "ssh -i $PROOF_SSH_KEY -o BatchMode=yes -o StrictHostKeyChecking=yes" \
            "$remote:$PROOF_PI_ROOT/results/$proof_id/" "$incoming/"
        "${deploy[@]}" install-results "${context[@]}" \
            --shards "$PROOF_SHARDS_ALL" --source "$incoming" \
            --results "$PROOF_RESULTS"
        ;;
    aggregate)
        output=${PROOF_AGGREGATION_OUTPUT:-"$PROOF_ROOT/aggregation/current"}
        "${deploy[@]}" aggregate "${context[@]}" \
            --shards "$PROOF_SHARDS_ALL" --results "$PROOF_RESULTS" \
            --output "$output"
        ;;
    central-gates)
        final_artifacts=${PROOF_FINALIZATION_ARTIFACTS:-"$PROOF_ROOT/finalization-artifacts"}
        mkdir -p "$final_artifacts"
        proof_python -c \
            'from pathlib import Path; import sys; Path(sys.argv[1]).unlink(missing_ok=True)' \
            "$final_artifacts/proof-summary.json"
        runtime=$(proof_container_runtime)
        : "${PROOF_RUNTIME_IMAGE:?PROOF_RUNTIME_IMAGE is required}"
        "$runtime" run --rm --pull never --network none --cap-drop ALL \
            --security-opt no-new-privileges \
            --user "$(id -u):$(id -g)" \
            --env DISTRIBUTED_PROOF_ID="$proof_id" \
            --volume "$final_artifacts:/workspace/app/artifacts" \
            "$PROOF_RUNTIME_IMAGE" bash -lc \
            'make baseline-in-container && make negative-tests-in-container && ./tests/test_unprivileged.sh && python3 tests/verify_fixtures.py --fixtures fixtures --output artifacts/fixture-verification.json && python3 tests/verify_state.py self-test'
        ;;
    finalize)
        output=${PROOF_AGGREGATION_OUTPUT:-"$PROOF_ROOT/aggregation/current"}
        source_summary="$output/determinism-summary.json"
        final_artifacts=${PROOF_FINALIZATION_ARTIFACTS:-"$PROOF_ROOT/finalization-artifacts"}
        [[ -s "$source_summary" ]] \
            || proof_die "distributed determinism summary is missing: $source_summary"
        "${deploy[@]}" validate-aggregation "${context[@]}" \
            --shards "$PROOF_SHARDS_ALL" --summary "$source_summary"
        mkdir -p "$final_artifacts"
        if [[ -e "$final_artifacts/determinism-summary.json" ]]; then
            cmp -s "$source_summary" "$final_artifacts/determinism-summary.json" \
                || proof_die 'existing finalization determinism summary differs'
        else
            install -m 0600 "$source_summary" \
                "$final_artifacts/determinism-summary.json"
        fi
        runtime=$(proof_container_runtime)
        : "${PROOF_RUNTIME_IMAGE:?PROOF_RUNTIME_IMAGE is required}"
        "$runtime" run --rm --pull never --network none --cap-drop ALL \
            --security-opt no-new-privileges \
            --user "$(id -u):$(id -g)" \
            --env DISTRIBUTED_PROOF_ID="$proof_id" \
            --volume "$final_artifacts:/workspace/app/artifacts" \
            "$PROOF_RUNTIME_IMAGE" bash -lc \
            'python3 tests/verify_state.py self-test && python3 tests/finalize_proof.py'
        ;;
    -h|--help|'') usage ;;
    *) proof_die "unknown proofctl command: $command_name" ;;
esac
