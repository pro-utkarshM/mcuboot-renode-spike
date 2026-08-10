# Physical distributed proof deployment

## Scope and current status

This deployment keeps the static-shard proof contract unchanged:

```text
laptop --WiFi--> proof-x86 --10.42.0.0/24--> proof-pi
```

`proof-x86` owns the manifest, assignments, collected evidence, aggregation,
and finalization. `proof-pi` owns only its ARM64 checkpoint materializations,
ready shards, journals, local immutable evidence, and failure diagnostics. The
laptop is not in the execution path after setup.

The scripts are implemented and locally tested, but neither physical worker was
reachable. The Pi is therefore **not qualified**, no worker-count recommendation
is recorded, and no production ARM64 image is claimed. The checked-in Dockerfile
is amd64-only and the optimized persistent executor still lives outside the
tracked production path. `setup-pi-worker.sh` consequently requires a frozen
ARM64 runtime archive and exact SHA-256 instead of constructing a weaker image.

## Host layout

Both hosts use a configurable `PROOF_ROOT`, conventionally `/srv/ota-proof`:

```text
control/                 frozen manifest, inputs, and lineage materializations
checkpoints/             architecture-local native snapshots
shards/all/              x86-owned complete assignment set
shards/ready/            validated shards runnable on this worker
transfer/incoming/       untrusted/incomplete transfer staging
results/                 validated immutable local or collected PASS envelopes
journals/                mutable resume state
failures/                complete failed-case diagnostics
aggregation/             x86-only reconstructed matrices and summaries
qualification/           worker qualification result
benchmarks/              raw measurements and recommendation
state/locks/             local shard exclusion locks
logs/                    operator logs
```

All mutable directories are mode `0700`. The frozen control bundle is made
non-writable. Transfer staging is outside `results/` and `shards/ready/`, so an
interrupted rsync cannot become runnable or aggregatable.

## Direct Ethernet

Choose the Ethernet interface explicitly. Never point the script at WiFi.

On x86:

```bash
sudo ./scripts/distributed/configure-proof-link.sh \
  --role x86 --interface <x86-ethernet-interface> --apply --skip-peer-test
```

On Pi, through a local console for the initial setup:

```bash
sudo ./scripts/distributed/configure-proof-link.sh \
  --role pi --interface <pi-ethernet-interface> --apply --skip-peer-test \
  --require-no-default
```

Then test from x86:

```bash
ping -c 3 -I <x86-ethernet-interface> 10.42.0.2
ssh -o BatchMode=yes -o StrictHostKeyChecking=yes 10.42.0.2 true
```

The script detects NetworkManager or systemd-networkd. It refuses ambiguous or
unsupported ownership, refuses a WiFi interface, uses static manual addressing,
disables DHCP/IPv6/default routes, and verifies that the existing WiFi default
route did not change. It never uses NetworkManager `shared` mode, forwarding,
bridging, masquerading, or NAT. ProxyJump needs SSH TCP forwarding, not kernel IP
forwarding.

If the host uses another manager such as an unsupported Netplan renderer, the
script exits without changing it and prints the required manual action.

## SSH

1. Generate a dedicated Ed25519 key on the laptop and a separate x86-to-Pi key:

   ```bash
   ssh-keygen -t ed25519 -f ~/.ssh/proof_ed25519
   ssh-keygen -t ed25519 -f ~/.ssh/proof_pi_ed25519
   ```

2. Install public keys normally. Verify both host fingerprints out of band and
   populate `known_hosts`; never disable host-key checking.
3. Adapt `deployment/ssh_config.example` with the x86 WiFi address and users.

The laptop can then use:

```bash
ssh proof-x86
ssh proof-pi
```

`proof-pi` uses `ProxyJump proof-x86`. The x86 SSH server must allow local TCP
forwarding to `10.42.0.2:22`. Do not use an authorized-key `restrict` option on
the laptop key because it disables forwarding; constrain it with
`PermitOpen 10.42.0.2:22`, disable agent/X11 forwarding, and retain public-key
authentication.

## Frozen inputs and setup

Create separate mode-0600 configurations from
`deployment/proof-worker.env.example`. Required values must come from the frozen
manifest and runtime build, not from host discovery.

When using `/srv/ota-proof`, create it once without granting worker privileges:

```bash
sudo install -d -o <proof-user> -g <proof-user> -m 0700 /srv/ota-proof
sudo install -d -o <proof-user> -g <proof-user> -m 0700 /etc/ota-proof
sudo install -o <proof-user> -g <proof-user> -m 0600 \
  worker.env /etc/ota-proof/worker.env
```

The control archive contains this structure:

```text
manifest.json
lineages/
profiles/
<all manifest-declared architecture-neutral inputs at their declared paths>
```

### x86

```bash
./scripts/distributed/setup-x86-worker.sh \
  --config /etc/ota-proof/worker.env \
  --install-control-bundle --load-runtime --checkpoints
```

This checks x86_64, minimum resources, Python/SSH/rsync/container runtime,
runtime archive hash, image architecture/ID, manifest input hashes, local
environment hashes, registered worker/build/executor/runtime-image identity,
and Pi key-based SSH.
It never installs or upgrades packages. Missing commands are reported for an
administrator to install from the host's pinned repository or image.

`--build-runtime` is an explicit x86-only alternative. It uses the digest-pinned
checked-in Dockerfile with `--pull=false`; absent bases fail instead of silently
pulling different dependencies.

### Pi

Copy the frozen ARM64 runtime and control archives to the Pi, then run:

```bash
./scripts/distributed/setup-pi-worker.sh \
  --config /etc/ota-proof/worker.env \
  --install-control-bundle --load-runtime --checkpoints
```

The Pi script verifies aarch64, Raspberry Pi 5 model, RAM/disk, runtime archive
hash, image architecture/ID, worker/build identity, and every manifest input.
It reports temperature and throttling when `vcgencmd` is available. It never
rebuilds guest firmware. The sealed ARM64 image must already contain the exact
Renode build, custom assemblies, ARM64 `mcumgr`, controller, and executor.

`PROOF_CHECKPOINT_PROGRAM` must generate the five Pi-local materializations and
their manifests. Real shards remain blocked until the immutable qualification
result configured by `PROOF_QUALIFICATION_RESULT` exists and validates.

## Qualification

Generate qualification shards for both registered workers, run them, and retain
separate x86 reference and Pi candidate result trees. The required cuts default
to:

```text
1, 4321, 9000, 10163, 10164, 10165, 10166, 15355, 30695, 30709
```

Adjust the set if necessary so it contains both an erase and a program operation.
Generate the paired, non-proof qualification assignments on x86 with:

```bash
python3 tests/distributed/generate_qualification.py \
  --manifest /srv/ota-proof/control/manifest.json \
  --lineages-dir /srv/ota-proof/control/lineages \
  --reference-worker proof-x86 --candidate-worker proof-pi \
  --output /srv/ota-proof/shards/qualification
```

Run each generated shard only on its named worker, retaining the x86 and Pi
result trees separately. First stage only that worker's `qualification_only`
shard into `PROOF_QUALIFICATION_READY`, then run:

```bash
./scripts/distributed/proofctl.sh \
  --config /etc/ota-proof/worker.env qualification-resume
```

An ARM worker without a qualification result may execute this queue, but the
runner rejects any non-qualification shard in it. The ordinary `resume` command
still fails until a passing qualification file is installed. Qualification
results are never mixed into the real matrix. Then run on x86 with both trees
available:
Then run on the Pi or x86 control node with both trees available:

```bash
./scripts/distributed/qualify-worker.sh --config /etc/ota-proof/worker.env
```

Qualification validates every envelope against the manifest/shard/worker, then
compares operation identity, full trace hash, fault evidence, committed flash,
final flash, semantic UART, MCUmgr, boots, and verifier JSON. Raw UART is the only
excluded field, matching the existing determinism contract. Missing evidence,
duplicate evidence, an absent erase/program boundary, or any divergence fails.

## Benchmarking and shard generation

The sealed benchmark harness receives `{phase}`, `{workers}`, `{sample}`, and
`{output}` and must write `metrics.json` containing case count, wall time, CPU,
peak RSS, maximum descriptors, stability, and optional temperature. It must also
report boolean throttling, memory-pressure, and descriptor-leak observations;
any such condition is ineligible for recommendation.

On x86:

```bash
./scripts/distributed/benchmark-worker.sh \
  --config /etc/ota-proof/worker.env --worker-counts 1,2,3,4
```

On Pi, test safe counts until throughput falls, memory pressure or thermal
throttling appears, or stability fails:

```bash
./scripts/distributed/benchmark-worker.sh \
  --config /etc/ota-proof/worker.env --worker-counts 1,2,3,4
```

At least three upload and three swap samples are required per count. Throttled
counts are ineligible. The output records all samples and recommends the count
with the lowest measured weighted repetition time (`10,165` upload/image-test
cuts and `20,544` swap cuts). Feed those measured phase rates into the existing
static shard generator with a target of 900 to 1800 seconds.

The generator now supports one logical root lineage per repetition with separate
x86 and ARM64 checkpoint materializations below it. It may therefore balance
cuts from the same repetition across the two hosts without sharing native
snapshots or changing work identity.

Every materialization records the snapshot path and SHA-256 for every cut. The
runner hashes the selected snapshot immediately before execution. Its checkpoint
identity binds those bytes, architecture, Renode build, execution mode, and the
repetition's common verified clean root. Architecture-specific executor commands,
runtime image IDs, and benchmark harness commands are also frozen by the proof
manifest; an ARM shard cannot silently run the x86 executor or image.

## Operation

On x86, promote local assignments and send Pi assignments:

```bash
./scripts/distributed/proofctl.sh --config /etc/ota-proof/worker.env install-local
./scripts/distributed/proofctl.sh --config /etc/ota-proof/worker.env push-pi
```

Run/resume workers manually:

```bash
# x86
./scripts/distributed/proofctl.sh --config /etc/ota-proof/worker.env resume

# Pi
./scripts/distributed/proofctl.sh --config /etc/ota-proof/worker.env resume
```

Show local progress:

```bash
./scripts/distributed/proofctl.sh --config /etc/ota-proof/worker.env status
./scripts/distributed/proofctl.sh --config /etc/ota-proof/worker.env pending-shards
./scripts/distributed/proofctl.sh --config /etc/ota-proof/worker.env completed-shards
```

Collect Pi evidence and aggregate on x86:

```bash
./scripts/distributed/proofctl.sh --config /etc/ota-proof/worker.env collect-pi
./scripts/distributed/proofctl.sh --config /etc/ota-proof/worker.env aggregate
```

Run the unchanged central baseline, negative-control, fixture, fault-hook,
offline/unprivileged, and verifier-self-test gates, then finalize:

```bash
./scripts/distributed/proofctl.sh --config /etc/ota-proof/worker.env central-gates
./scripts/distributed/proofctl.sh --config /etc/ota-proof/worker.env finalize
```

This installs the immutable distributed determinism summary without overwrite,
starts the pinned image with no network, no capabilities, and no-new-privileges,
runs the verifier self-test, and invokes the existing `finalize_proof.py`. Every
central gate and the final `PROVEN` summary is bound to the same `proof_id`; a
stale gate from another distributed run is rejected.

Rsync uses SSH, `--ignore-existing`, a private partial directory, and delayed
renames. Received shards and results are then independently parsed, hashed, and
validated before immutable promotion. A correct-looking filename is never
sufficient. No transfer command uses `--inplace` or `--delete`.

The LAN exists only around transfer. Every production executor manifest must
bind `network=none`, `cap_drop=ALL`, `no_new_privileges=true`, and a non-root
runtime user. `run_shard.py` invokes only the exact manifest command. The current
Docker proof gate continues to inspect the actual runtime namespace and
capabilities.

## Optional systemd operation

Manual `resume` is sufficient. For reboot persistence, install the optional
unprivileged user units on each worker:

```bash
./scripts/distributed/install-systemd-user.sh \
  --config /etc/ota-proof/worker.env
sudo loginctl enable-linger <proof-user>
```

The installer enables exactly `PROOF_WORKER_JOBS` slots. Each slot owns a stable
subset of shard IDs and takes a nonblocking per-shard lock. Stop all slots before
changing the count. A validated case failure exits with status 2 and is excluded
from automatic restart; an operator must inspect the retained failure bundle.
Unexpected process crashes still restart. Logs are available through:

```bash
journalctl --user -u 'proof-worker@*.service' -f
```

The service runs without privilege escalation, restarts only on failure, stops
on SIGTERM, and uses immutable PASS evidence rather than mutable journal state
to decide whether a case must run.

## Failure behavior

- Laptop/router outage: both hosts continue already received work.
- Direct cable/SSH outage: Pi continues ready shards; collection waits.
- Reboot/worker crash: RUNNING work reruns, validated PASS work does not.
- Interrupted transfer: partial files remain outside ready/results trees.
- Duplicate/stale/wrong-build result: central promotion fails closed.
- Incomplete proof: aggregation fails and cannot emit `PROVEN`.
- x86 unavailable: Pi finishes local work but cannot receive or return shards.

The distributed aggregator emits `distributed-matrix-summary.json` plus the
existing verifier's unchanged `determinism-summary.json`. On x86, place that
determinism gate alongside independently executed baseline, negative-control,
fixture, fault-hook, unprivileged/offline, and verifier-self-test gates, then
run the existing trusted finalizer. Workers cannot emit `proof-summary.json` or
`PROVEN`.

## Physical bring-up order

1. Connect the direct cable and, from the Pi console, apply its static link:

   ```bash
   sudo ./scripts/distributed/configure-proof-link.sh \
     --role pi --interface <pi-ethernet> --apply --skip-peer-test \
     --require-no-default
   ```

2. On x86, apply its static link without touching WiFi, then verify the path:

   ```bash
   sudo ./scripts/distributed/configure-proof-link.sh \
     --role x86 --interface <x86-ethernet> --apply
   ip route get 10.42.0.2
   ssh -o BatchMode=yes -o StrictHostKeyChecking=yes 10.42.0.2 true
   ```

3. Install the frozen x86 control/runtime artifacts and checkpoints:

   ```bash
   ./scripts/distributed/setup-x86-worker.sh \
     --config /etc/ota-proof/worker.env \
     --install-control-bundle --load-runtime --checkpoints
   ```

4. Copy the hash-pinned control and ARM64 runtime archives from x86 to Pi, then
   run on Pi:

   ```bash
   ./scripts/distributed/setup-pi-worker.sh \
     --config /etc/ota-proof/worker.env \
     --install-control-bundle --load-runtime --checkpoints
   ```

5. Generate and stage worker-specific qualification shards, run
   `qualification-resume` on both architectures, collect the two result trees on
   x86, and run `qualify-worker.sh`. Copy the immutable qualification result to
   the configured Pi path. Do not promote real Pi shards before it validates.

6. Benchmark x86 counts `1,2,3,4` and Pi safe counts, update the frozen worker
   profiles with measured phase rates, then generate 900-1800 second static
   shards with `tests/distributed/generate_shards.py`.

7. On x86, promote/send shards and start both workers:

   ```bash
   ./scripts/distributed/proofctl.sh --config /etc/ota-proof/worker.env install-local
   ./scripts/distributed/proofctl.sh --config /etc/ota-proof/worker.env push-pi
   ./scripts/distributed/proofctl.sh --config /etc/ota-proof/worker.env resume
   ssh 10.42.0.2 '/opt/ota-proof/repo/scripts/distributed/proofctl.sh --config /etc/ota-proof/worker.env resume'
   ```

8. Optionally install the user services on both machines and enable lingering.
   The laptop may then disconnect; x86 and Pi continue from their local queues.

## Upstream references

- NetworkManager manual IPv4 mode and `never-default` behavior:
  https://www.networkmanager.dev/docs/api/latest/nm-settings-nmcli.html
- systemd-networkd configuration:
  https://www.freedesktop.org/software/systemd/man/latest/systemd.network.html
- OpenSSH `ProxyJump` and strict host-key checking:
  https://man.openbsd.org/ssh_config#ProxyJump
- rsync temporary files and delayed update behavior:
  https://rsync.samba.org/ftp/rsync/rsync.1.html
- Docker network/capability isolation:
  https://docs.docker.com/reference/cli/docker/container/run
- Podman `--network none` behavior:
  https://docs.podman.io/en/latest/markdown/podman-run.1.html
