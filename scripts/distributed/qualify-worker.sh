#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=common.sh
source "$SCRIPT_DIR/common.sh"

[[ ${1:-} == --config ]] || proof_die 'usage: qualify-worker.sh --config FILE'
config=${2:?configuration path is required}
proof_load_config "$config"
: "${PROOF_QUALIFICATION_SHARDS:?PROOF_QUALIFICATION_SHARDS is required}"
: "${PROOF_REFERENCE_RESULTS:?PROOF_REFERENCE_RESULTS is required}"
: "${PROOF_CANDIDATE_RESULTS:?PROOF_CANDIDATE_RESULTS is required}"
: "${PROOF_REFERENCE_WORKER_ID:?PROOF_REFERENCE_WORKER_ID is required}"

output=${PROOF_QUALIFICATION_RESULT:-"$PROOF_ROOT/qualification/worker.json"}
proof_python "$PROOF_REPO/tests/distributed/qualify_worker.py" \
    --manifest "$PROOF_MANIFEST" \
    --lineages-dir "$PROOF_LINEAGES_DIR" \
    --shards "$PROOF_QUALIFICATION_SHARDS" \
    --reference-results "$PROOF_REFERENCE_RESULTS" \
    --candidate-results "$PROOF_CANDIDATE_RESULTS" \
    --reference-worker "$PROOF_REFERENCE_WORKER_ID" \
    --candidate-worker "$PROOF_WORKER_ID" \
    --repetition "${PROOF_QUALIFICATION_REPETITION:-1}" \
    --cuts "${PROOF_QUALIFICATION_CUTS:-1,4321,9000,10163,10164,10165,10166,15355,30695,30709}" \
    --output "$output"
