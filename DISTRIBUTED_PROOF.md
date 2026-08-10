# Distributed proof runner investigation

## Status

This document designs an opt-in distributed runner. It does not replace the
reference `make proof` path and it does not change the proof contract.

The repository's production runner is still the fresh-process implementation in
`tests/run_matrix.sh`. The checkpoint and persistent-worker programs used for
performance experiments are currently under ignored `artifacts/`; they are not
part of the checked-in proof runner. A distributed production rollout must first
promote a differentially verified executor behind the same per-case evidence
contract.

The two auxiliary machines were not reachable during this investigation. A LAN
scan on 2026-08-10 found only the router and the 16-core development host.
Consequently, this report does not claim spare-x86 or Pi 5 throughput or Pi
qualification. It defines the exact qualification and benchmark gates needed to
obtain those measurements.

The opt-in prototype was exercised in two ways:

- Two restricted, networkless containers ran real reference cases concurrently
  as logical workers A and B. Cut 1 and cut 10,166 both passed the existing
  verifier, produced current compact evidence, and validated as immutable result
  envelopes. The bounded smoke took 31 seconds on four pinned logical CPUs.
- A complete synthetic two-cut, five-repetition shard set was split across two
  logical x86 workers, resumed from immutable PASS results, aggregated into the
  current matrix format, and accepted by the existing matrix and determinism
  verifier. Raw UART hashes deliberately differed across repetitions and were
  correctly excluded, matching `tests/determinism.sh`.

This proves the protocol adapter locally, not physical cross-host execution.
The lineage schema now implements the two-level root/materialization design:
each repetition has one logical `root_lineage_id`, while x86 and ARM64 use
separate architecture/build/execution-mode `lineage_id` materializations. The
cost scheduler can assign different cuts in one repetition to either qualified
host without making native snapshots portable or changing case uniqueness.

The physical x86-control/Pi-worker setup, staged transfer protocol, resumable
worker loop, qualification comparator, benchmark adapter, and optional systemd
units are documented in `deployment/README.md`. Physical qualification and
throughput remain unmeasured because those hosts were unavailable.

## Distribution boundary

The safe unit of distribution is one complete fault execution identified by:

```text
(proof_id, repetition, cut)
```

The worker must execute the complete independently required suffix and run the
existing local verifier before publishing evidence. It cannot publish `PROVEN`.

### Globally shared and immutable

- Signed v1, v2, and negative-control firmware fixtures and MCUboot binaries.
- The canonical clean operation trace and its parsed operation list.
- `PROOF.md` and a versioned proof-contract declaration.
- Controller, verifier, fault-snapshot verifier, summarizer, and runner code.
- Platform descriptions, Renode scripts, custom peripheral source and pinned
  assembly.
- Allowed execution-mode definitions, including native RAM and optional ROMD.
- Renode source revision, patch set, managed assemblies, and native Tlib build
  identity for every qualified host architecture.
- Negative fixtures and verifier self-tests.

### Repetition-specific

- One logical root lineage for each repetition 1 through 5.
- Independently generated clean-execution record for that repetition.
- Architecture-local checkpoint materializations descended from that root.
- Matrix rows and compact evidence for the repetition.

### Architecture-specific

- Renode executable, `Infrastructure.dll`, Tlib/native libraries, runtime
  identifier, and ROMD patch build.
- Native snapshots and their checkpoint manifests.
- Architecture-specific checkpoint materialization ID.
- Qualified execution-mode hash and benchmark profile.

### Host-specific diagnostics

- Worker ID, CPU model, OS/kernel, memory, container runtime, worker count, and
  resource measurements. These do not change `proof_id`, but an unqualified
  execution environment is rejected.

### Case-specific

- Cut and repetition, selected checkpoint, operation tuple, local execution
  timing, trace, fault evidence, committed snapshot hash, final flash hash,
  semantic UART hash, raw UART diagnostic hash, MCUmgr hash, boot count, and
  verifier JSON.

## Immutable proof manifest

The manifest is canonical JSON. `proof_id` is:

```text
sha256(canonical_json(manifest_without_proof_id))
```

The manifest must contain at least:

```json
{
  "schema": "ota.distributed-proof.manifest",
  "version": 1,
  "repetitions": 5,
  "cut_count": 30709,
  "proof_contract_sha256": "...",
  "required_gates": [
    "baseline",
    "matrix",
    "determinism",
    "negative-tests",
    "offline-unprivileged",
    "verifier-self-test"
  ],
  "negative_fixtures": {
    "premature-confirm": "...",
    "erase-after-confirm": "..."
  },
  "inputs": [
    {"path": "path/or/logical-name", "sha256": "..."}
  ],
  "checkpoint_root": "../checkpoints",
  "canonical_trace": {
    "sha256": "...",
    "operations_sha256": "...",
    "operation_count": 30709
  },
  "lineage_seeds": ["...", "...", "...", "...", "..."],
  "renode_builds": {
    "linux-x86_64": {
      "build_identity": "renode-1.16.1-patched-x86_64",
      "sha256": "..."
    },
    "linux-aarch64": {
      "build_identity": "renode-1.16.1-patched-aarch64",
      "sha256": "..."
    }
  },
  "execution_modes": {
    "<native-ram-execution-mode-sha256>": {"qualified": true},
    "<native-ram-romd-execution-mode-sha256>": {"qualified": true}
  },
  "qualified_workers": {
    "spare-x86": {
      "architecture": "linux-x86_64",
      "build_identity": "renode-1.16.1-patched-x86_64",
      "executor_id": "executor-x86_64",
      "runtime_image_id": "sha256:...",
      "attestation_hashes": {
        "environment_hash": "...",
        "offline_hash": "...",
        "unprivileged_hash": "..."
      }
    }
  },
  "executors": {
    "executor-x86_64": {
      "architecture": "linux-x86_64",
      "build_identity": "renode-1.16.1-patched-x86_64",
      "execution_mode_hash": "...",
      "argv": ["..."],
      "sha256": "sha256(canonical_json(argv))",
      "benchmark_argv_sha256": "...",
      "isolation": {
        "network": "none",
        "cap_drop": "ALL",
        "no_new_privileges": true,
        "runtime_user": "non-root"
      }
    }
  }
}
```

Inputs include the firmware binaries, clean trace, `PROOF.md`, `Dockerfile`,
`Makefile`, firmware manifest, controller, all acceptance scripts, verifier,
negative fixtures, `.repl`/`.resc` files, ROMD patch, custom peripheral source,
`FaultInjectingFlash.dll`, `Infrastructure.dll`, `Migrant.dll`, `ELFSharp.dll`,
Renode/Tlib native binaries, and proof configuration. Each architecture's OCI
image ID is also recorded, but it does not replace hashes of proof-relevant files.

A worker hashes its sealed local files and refuses a shard before starting any
case if a required hash, architecture build, execution mode, or lineage differs.
The prototype validates every one of these fields, the six required proof
gates, both named negative fixtures, five unique lineage seeds, and the exact
architecture-specific executor and benchmark commands before dispatching a case.

## Independent repetition lineage

Use two levels so a repetition remains singular while snapshots remain native
to each architecture:

```text
repetition root lineage
    +-- x86_64 checkpoint materialization
    +-- aarch64 checkpoint materialization
```

The root lineage ID binds `proof_id`, repetition number, a precommitted unique
lineage nonce, and that repetition's independently generated clean-execution
record. The materialization ID additionally binds architecture, Renode build,
execution mode, clean trace, baseline flash, checkpoint index, and every native
snapshot hash.

The aggregator requires exactly five distinct root lineage IDs, each permanently
bound to one repetition. A case may use only a qualified architecture-local
materialization under its repetition root. Repetition 2 may not load a snapshot
from repetition 1 even when the serialized bytes happen to be identical.

Firmware, the canonical fault-point definition, model binaries, and verifier can
be shared. Checkpoint generation and use cannot be shared across repetitions.
Content-addressed storage deduplication should not be enabled for checkpoint
blobs in the first production version because it obscures lineage auditing for
little storage benefit.

## Snapshot portability

Renode documents that snapshots can be transferred, but also warns that version
changes can make them incompatible. The current snapshot header records the host
runtime, for example:

```text
Renode, version 1.16.1 (...) running on Linux-X64 .NET 8.0.24
```

The custom flash models mark backing streams and trace writers transient and
require explicit post-load rebinding. The ROMD mode also reconstructs a native
CPU mapping after load. These custom/native details have not been tested across
x86_64 and aarch64. Therefore snapshots are architecture-local and
execution-build-local. Cross-architecture loading must remain disabled even if
a future experiment happens to load one snapshot successfully; qualifying it
would require the full representative differential suite and repeated restore.

Relevant upstream references:

- https://renode.readthedocs.io/en/latest/basic/saving.html
- https://github.com/renode/renode/releases/tag/v1.16.1

## Per-case evidence

A successful case is one canonical immutable JSON envelope. It binds:

```text
schema
proof_id
repetition
cut
root_lineage_id
lineage_id (architecture-local checkpoint materialization ID)
checkpoint_id
checkpoint_path and checkpoint_sha256
execution_mode_hash
worker architecture, build, executor, and runtime-image identity
offline/unprivileged attestation hashes
matrix row
final-state verification JSON
committed-operation verification JSON
hashes and sizes of raw case artifacts
timing/resource diagnostics
result_id
```

`result_id` is the SHA-256 of the canonical envelope without `result_id`. The
committed filename contains `result_id`. A mismatch is rejected.

For successful cases, required raw-artifact metadata is not an unstructured
hash bag. The envelope requires the final flash, operation trace, UART,
MCUmgr image list, committed fault snapshot, matrix row, compact evidence, both
verification documents, and committed fault-operation record. Their hashes are
cross-checked against the compact verifier fields before publication and again
during central aggregation.

Publication protocol:

1. Create a case-private staging directory on the same filesystem.
2. Run the real branch and existing verifier there.
3. Build the envelope only after verification succeeds.
4. Flush files, write a temporary envelope, flush it, and atomically link/rename
   to its immutable final name without overwrite.
5. Flush the parent directory.
6. Delete bulk success artifacts only after the immutable envelope exists.

Failures retain all raw flash, fault snapshot, trace, UART, MCUmgr, Renode log,
verifier output, process metrics, and a structured failure phase. A failure is
never converted into passing compact evidence.

The reference runner already trusts local verify-before-compaction and retains
hashes rather than all successful bulk artifacts. The distributed v1 preserves
that trust boundary. Content hashes prevent accidental/stale mixing, but they do
not prove that a malicious worker executed Renode. Host signatures or MACs prove
origin, not correctness, because a compromised worker can sign fabricated data.
Stronger adversarial-worker resistance requires either retaining raw evidence
for central re-verification or remote attestation; neither is needed to preserve
the current contract and both add substantial complexity.

## Static sharding

Version 1 uses no service:

```text
freeze proof manifest and lineages
generate many small immutable shard manifests
copy shards and sealed execution image to qualified workers
run each shard locally and resumably
copy immutable result envelopes back
aggregate centrally
```

Use enough shards that one shard represents roughly 15 to 30 minutes on its
assigned profile. This bounds stranded work when the main machine leaves during
the day. Do not run the same shard on two hosts concurrently.

Local state is derived from files:

```text
PENDING  no journal and no result
RUNNING  journal exists, no valid result
PASS     one valid immutable result exists
FAIL     immutable failure bundle exists
```

After a crash, an old RUNNING journal is diagnostic only and the case may rerun.
A valid PASS is revalidated and skipped. Static v1 has no lease because no
always-on coordinator owns time; reassignment is a deliberate copy/ownership
operation. If automatic leases and daytime worker join/leave become operational
requirements, add a small coordinator later.

## Cost-aware scheduling

The scheduler uses phase-specific measured costs. At minimum it separates:

```text
upload/image-test: cuts 1..10165
swap:              cuts 10166..30709
```

Prefer historical medians in smaller cut buckets and split program/erase
operations when enough measurements exist. Each worker profile records separate
upload and swap rates because the Pi-to-x86 ratio may differ by phase.

For static host assignment, use greedy scheduling on unrelated machines:

1. Sort work units by descending maximum predicted duration.
2. For each unit, compute the projected finish time on every compatible host
   using that host's phase-specific measured rate.
3. Assign it to the host with the earliest projected finish.
4. Pack each host's assignments into bounded-duration shards while keeping all
   work identities explicit.

This balances estimated wall time, not cut count. The generator and all worker
profiles are included in the shard manifest, making the assignment auditable.

## ARM64 and Pi 5 qualification

The current OCI image cannot run natively on the Pi:

- `antmicro/renode:1.16.1` is a single-platform amd64 image.
- `Dockerfile` explicitly asserts `dpkg --print-architecture = amd64`.
- It downloads x86_64 Zephyr host tools and toolchains.

Renode 1.16.1 does provide an official native Linux ARM64 portable package and
states native AArch64 Linux support. The release asset is
`renode-1.16.1.linux-arm64-portable-dotnet.tar.gz`, with upstream SHA-256
`fff3a098c96ed0a4ffbdff3f028c9c5fde432db09587c7bd7c99406180f90007`.

The smallest Pi runtime image should not rebuild firmware. It should:

1. Start from a pinned arm64 Ubuntu/.NET runtime image.
2. Install the pinned ARM64 Renode package, or build the exact patched Renode
   revision natively when ROMD is enabled.
3. Build/copy the AnyCPU custom peripheral assembly against that exact Renode
   build and record its hash.
4. Build a static ARM64 `mcumgr` from the pinned commit.
5. Copy the already built, hash-verified architecture-neutral firmware fixtures
   and proof code from the immutable proof-input bundle.
6. Run with the same network-none, non-root, no-capabilities restrictions.

Before real work, generate five Pi-local checkpoint materializations and run the
qualification cuts covering early/middle/late upload, image-test boundary,
early/middle/late swap, erase, program, and final cut. Compare complete trace,
fault evidence, at-fault flash, final flash, semantic UART, MCUmgr, boot count,
and verifier JSON to x86. Raw UART transport bytes remain diagnostic only.

## Aggregation and finalization

The central aggregator:

1. Recomputes `proof_id`, validates the full manifest, and validates registered
   root lineages/materializations.
2. Scans immutable envelopes and rejects malformed, stale, or partial files.
3. Rejects every duplicate `(proof_id, repetition, cut)`, including identical
   duplicates, so accidental concurrent execution is visible.
4. Requires every cut 1 through 30,709 exactly once in every repetition 1
   through 5.
5. Reconstructs the five current-format matrix CSVs and evidence JSONL files in
   cut order.
6. Runs the existing `validate-matrix` and `compare-matrix` checks, including all
   current deterministic hash columns.
7. Produces a distributed matrix summary, never `proof-summary.json`.

Only the trusted central finalization path may then combine the distributed
matrix summary with baseline, negative-control, offline/unprivileged, fixture,
fault-hook, and verifier-self-test gates. The opt-in distributed finalization
adapter adds `proof_id` to every gate and the existing finalizer rejects a stale
or mixed gate before it can emit `PROVEN`; the reference path is unchanged when
`DISTRIBUTED_PROOF_ID` is absent.

## Dynamic coordinator decision

A coordinator is not justified for three machines initially. Small static
shards, local immutable PASS publication, and explicit collection provide
resumability with a much smaller audit surface.

Add a coordinator only if manual shard ownership becomes unreliable. Its state
machine would use expiring leases for CLAIMED/RUNNING, but immutable PASS objects
would remain the source of truth. SQLite plus an append-only object directory is
sufficient; a network service, queue, database cluster, and worker-trusted
finalization are not.

## Measured throughput and projection thresholds

Available measurements on the 16-core Ryzen 7 7735HS:

| Mode | Workers | Workload | Throughput | Peak RSS/worker |
| --- | ---: | --- | ---: | ---: |
| Native RAM + ROMD persistent | 1 | 5 warmed middle-swap branches | 0.333 cases/s | 333 MiB |
| Native RAM + ROMD persistent | 8 | 48 middle-swap branches | 2.106 cases/s | 347 MiB |

These are swap-only measurements and must not be used as whole-matrix rates.
The measured weighted projection of 31 to 34 hours implies a main-host full
matrix rate of 1.254 to 1.376 cases/s after fixed overhead.

Let `S` be the measured combined full-matrix rate of the spare x86 and Pi, and
let `M` be the main rate. Then:

```text
scenario A: seconds = 153545 / S
scenario B: solve 153545 = S*T + M*available_main_seconds(T)
scenario C: seconds = 153545 / (S + M)
```

For completion in 24 hours:

| Scenario | Required measured continuous auxiliary rate |
| --- | ---: |
| A, auxiliaries only | 1.777 cases/s |
| B, main contributes 8 h | 1.318 to 1.359 cases/s |
| B, main contributes 10 h | 1.204 to 1.255 cases/s |
| C, all continuous | 0.401 to 0.523 cases/s |

The spare and Pi rates are intentionally blank until each machine runs the same
weighted benchmark at candidate worker counts. On the spare, benchmark 1, 2, 3,
and 4 persistent workers. On the Pi, first qualify correctness, then benchmark
worker counts until throughput falls or memory/thermal throttling appears.
Record upload and swap time, cases/s, CPU utilization, RSS, descriptors, worker
stability, temperature, and clock throttling.

## Production sequence

1. Land the manifest, lineage, evidence-envelope, shard-generator, local runner,
   and aggregation library without changing `make proof`.
2. Promote the existing x86 persistent executor from ignored experiments behind
   an opt-in adapter and run differential tests against fresh-process cases.
3. Generate five independent x86 lineage roots/materializations and execute a
   two-logical-worker static-shard smoke test; reconstruct current matrices and
   pass the existing verifier.
4. Make the 4-core x86 host reachable, qualify its sealed environment, benchmark
   1 through 4 workers, and run a small returned shard.
5. Build the pinned ARM64 runtime image, generate Pi-local materializations, run
   the full cross-architecture qualification set, and benchmark safe worker
   counts. Keep the Pi out of proof work until every comparison passes.
6. Choose 15 to 30 minute cost-balanced shards from measured profiles and run a
   bounded multi-host repetition before the full matrix.
7. Run the implemented proof-ID-bound central gate adapter and distributed
   summary integration; only the trusted central finalizer may emit `PROVEN`.
