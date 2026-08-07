# Proof record

## Current verdict

```text
NOT PROVEN
```

The implementation exists, but this checked-in record is not upgraded to
`PROVEN` until the complete offline `make proof` execution succeeds. A failed
or interrupted build, baseline gate, cut-point run, repetition, negative
control, or privilege check keeps this verdict at `NOT PROVEN`.

## Pinned inputs

| Component | Pin |
| --- | --- |
| Zephyr | `v4.4.0`, commit `684c9e8f32e4373a21098559f748f06915f950c9` |
| MCUboot | `ee39e2d694bd827ffd1bebbce2f571a9154e6ec2` (Zephyr manifest) |
| Nordic HAL | `44fd3d44b15cb75f80a25b4679f91d2787e28664` (Zephyr manifest) |
| Zephyr SDK | `v1.0.1`, verified host-tools and ARM archive SHA-256 values in `Dockerfile` |
| Renode | `v1.16.1`, official Antmicro image digest `sha256:fc2a8c1bad2296a6d7cbc852bbf5540b22b778bdeb0ad42a45b8c54ea1e6a24c` |
| MCUmgr CLI | `5c56bd24066c780aad5836429bfa2ecc4f9a944c` |

## Architecture and fidelity

The target is upstream `nrf52840dk/nrf52840`. West sysbuild produces a real
MCUboot domain and imgtool-signed application image. MCUboot boots v1, Zephyr's
SMP server accepts Apache MCUmgr image upload/test/confirm commands on UART0,
and Renode exposes UART0 as a PTY in a session-private temporary directory. OTA
bytes therefore enter the secondary slot through the guest Zephyr flash driver;
only factory provisioning assembles MCUboot and v1 directly into a blank 1 MiB
flash image.

The application-domain sysbuild overlays disable the DK's external QSPI flash
controller and child device. The OTA slots, settings partition, MCUboot, and
custom Renode flash model all use the nRF52840's internal NVMC flash; the
external part is neither accessed by the spike nor present in `platform.repl`.

The final image layer makes the controller, verifier, Renode model, signed
fixtures, sealed v1 image, and their parent directories root-owned. The proof
runs as UID 10001 and cannot rewrite those proof inputs. Baseline initialization
therefore writes its generated flash image under `artifacts/`, not back into
the sealed `fixtures/` directory.

Renode's stock nRF52840 platform maps code flash as `MappedMemory` and omits an
NVMC implementation. That cannot expose page erase and word-program boundaries
to C#. `renode/FaultInjectingFlash.cs` replaces it with `ArrayMemory`, models
NVMC mode/ready/erase registers, enforces one-to-zero programming, and persists
each operation through one unbuffered backing-file stream. The selected fault
path flushes that stream before copying its committed snapshot. It deliberately
does not call host stable-media `fsync`: the graded event is simulated-MCU power
loss, not failure of the Docker host.

At the configured completed operation the model records evidence and copies a
1 MiB committed snapshot, clears the modeled RAM array, then calls Renode's
machine reset request. CPU, timers, UART and volatile peripherals follow their
ordinary Renode reset paths. Flash storage, the operation counter, and the
one-shot fired latch intentionally survive. The model does not emulate analog
partial-bit behavior during an electrical pulse.

## Acceptance evidence contract

`make proof` must establish, in order:

1. v1 boots and initializes persistent state through MCUboot.
2. V2 uploads through MCUmgr, boots as a test image, is confirmed, and remains
   v2 across three further resets.
3. The same baseline and unconfirmed v2 revert to v1.
4. The hook-selected program is present in the at-cut snapshot, the immediate
   UART continuation sentinel is absent before the reset-vector marker,
   volatile RAM reports one, persistent state survives, and the cut fires once.
5. Every operation in the clean ordered trace is used as a cut point; each run
   restores the identical baseline and converges to a verified v1 or v2. Full
   transient evidence is checked before compact hashes and verification JSON
   are checkpointed in cut order.
6. Five complete repetitions have identical traces, outcomes, boot counts, and
   relevant flash, trace, semantic-UART, MCUmgr, and fault-snapshot hashes, with
   no hangs or unrecoverable states. Raw UART bytes are retained as a diagnostic
   hash but excluded because SMP packet interleaving varies independently of
   the ordered firmware markers.
7. Premature-confirm and erase-after-confirm variants are both rejected at a
   deterministic cut point.
8. The runtime is non-root, every capability set is zero, only loopback
   networking exists, and KVM, TAP, physical serial and Docker socket are absent.
   The same gate asserts that signed fixtures, the sealed v1 image, controller,
   verifier, and Renode fault model are not writable by the runtime user.

Successful execution creates the required directories and JSON/CSV records
under `artifacts/`, ending with `artifacts/proof-summary.json`. That aggregator
can write `"verdict": "PROVEN"` only after all preceding verified summaries
exist and report pass. Both host and container proof entry points remove any
older final summary before starting, and the aggregator publishes its new
summary with an atomic rename, so an interrupted rerun cannot retain or expose
a stale `PROVEN` verdict.

## Latest executed gates

On 2026-08-07, the restrictive offline container invocation passed the baseline
confirmed-update, repeated-boot, revert, selected-fault, committed-snapshot,
negative-control, and unprivileged-runtime gates. The clean auto-confirm trace
contained 30,709 operations.

A deliberately bounded top-level run, `MATRIX_BATCH_LIMIT=1 make proof`, passed
the baseline, checkpointed cuts 1 through 8, and then returned nonzero through
Make because 30,701 cuts and all five complete repetitions still remained. Its
partial matrix validated as contiguous and passing, its compact evidence had
exactly eight records, and `artifacts/proof-summary.json` was absent. This is a
runner/resume safety check, not a substitute for the complete proof.

After sealing the proof inputs against UID 10001 writes and relocating the
generated initialized flash to `artifacts/baseline/`, the baseline, both
negative controls, and the expanded unprivileged gate passed again. Two resumed
batches advanced the contiguous standalone checkpoint through cut 24 and again
returned the intentional incomplete status. The later top-level run began with
a deliberately seeded stale `PROVEN` summary; the proof entry point removed it
before building and did not publish a replacement after the bounded failure.

## Known limitations

- The model covers the NVMC register sequences used by the pinned nRF52840
  Zephyr/MCUboot binaries, not every nRF52840 NVMC feature or timing detail.
- A program boundary is the completed 1-, 2-, or 4-byte access delivered by
  Renode to the flash peripheral. The pinned Nordic driver is expected to use
  aligned 32-bit stores; the clean trace records the actual lengths.
- Flash bytes change atomically at the selected completed-operation boundary.
  Mid-pulse analog corruption is explicitly outside the graded model.
- The included signing key is an upstream test key, not production key
  material.
- The exhaustive matrix can be large. This record reports only actual measured
  counts; it never substitutes estimated or fabricated totals. The current
  40,640-byte signed auto-confirm image produces a clean trace of 30,709
  completed operations. The complete five-repetition matrix has not run, so
  the verdict remains `NOT PROVEN`. A resumed eight-cut checkpoint took 62.68
  seconds on the current 16-core host. At that measured rate, five complete
  repetitions project to about 13.92 days. Sixteen concurrent sessions were
  slower, so eight remains the measured default.
