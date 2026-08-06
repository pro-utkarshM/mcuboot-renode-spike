# Zephyr + MCUboot + Renode power-loss spike

This repository is a clean-room feasibility spike for one claim: a real
Zephyr/MCUboot serial OTA can run in a restricted container while a custom
Renode nRF52840 flash model cuts power after an exact completed guest flash
operation, retains nonvolatile bytes, and restarts from reset.

It uses Zephyr sysbuild and imgtool to build MCUboot plus signed applications,
Apache `mcumgr` over a Renode-created UART PTY, and an `ArrayMemory`-backed C#
NVMC/flash model. Python is limited to orchestration and evidence checking; it
does not implement a bootloader, image swap, flash driver, or OTA transport.

## Run

```sh
make image
make baseline
make matrix
make determinism
make negative-tests
make prove-unprivileged
make proof
```

`make image` is the only networked phase. Every run command starts the built
image with no network, all Linux capabilities dropped, and
`no-new-privileges`. The exact final restrictive invocation is:

```sh
docker run --rm --network none --cap-drop ALL \
  --security-opt no-new-privileges mcuboot-renode-spike:latest
```

The complete matrix is intentionally exhaustive. The model treats each guest
NVMC program bus transaction as an operation; it does not combine word writes
to make the run count smaller. Runtime is therefore proportional to the actual
signed image size and observed operation count.

## Boundaries and layout

- Zephyr `v4.4.0`, MCUboot and HAL revisions imported by its manifest
- Zephyr SDK `v1.0.1`, ARM toolchain only
- Renode `v1.16.1`
- Apache `mynewt-mcumgr-cli` at a fixed commit
- upstream `nrf52840dk/nrf52840` partition map
- flash at `0x00000000`, 1 MiB total, 4 KiB erase pages
- MCUboot at `0x00000000`, primary slot at `0x0000c000`

The RSA key under `keys/` is MCUboot's public upstream development/test key.
It is deliberately reproducible and is not suitable for a production trust
root.

See [PROOF.md](PROOF.md) for the evidence contract and current verdict.
