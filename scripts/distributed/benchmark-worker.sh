#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=common.sh
source "$SCRIPT_DIR/common.sh"

config= counts=
while (($#)); do
    case "$1" in
        --config) config=${2:?}; shift 2 ;;
        --worker-counts) counts=${2:?}; shift 2 ;;
        *) proof_die "unknown argument: $1" ;;
    esac
done
[[ -n "$config" ]] || proof_die 'usage: benchmark-worker.sh --config FILE --worker-counts LIST'
proof_load_config "$config"
: "${PROOF_BENCHMARK_HARNESS:?PROOF_BENCHMARK_HARNESS is required}"
: "${PROOF_WORKER_ARCH:?PROOF_WORKER_ARCH is required}"
: "${PROOF_BUILD_IDENTITY:?PROOF_BUILD_IDENTITY is required}"
counts=${counts:-1,2,3,4}
run_dir="$PROOF_ROOT/benchmarks/run-$(date -u +%Y%m%dT%H%M%SZ)"
summary="$PROOF_ROOT/benchmarks/summary-$(date -u +%Y%m%dT%H%M%SZ).json"

proof_python "$PROOF_REPO/tests/distributed/benchmark_worker.py" \
    --worker-id "$PROOF_WORKER_ID" \
    --manifest "$PROOF_MANIFEST" \
    --executor-id "$PROOF_EXECUTOR_ID" \
    --architecture "$PROOF_WORKER_ARCH" \
    --build-identity "$PROOF_BUILD_IDENTITY" \
    --command "$PROOF_BENCHMARK_HARNESS" \
    --worker-counts "$counts" \
    --samples "${PROOF_BENCHMARK_SAMPLES:-3}" \
    --output-dir "$run_dir" \
    --summary "$summary"
