#!/usr/bin/env python3
"""Focused opt-in checks for NativePowerLossRam.

This test is intentionally separate from the reference proof path. It expects a
Renode installation containing the pinned FaultInjectingFlash assembly built by
this repository.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import subprocess
import tempfile
import time
from pathlib import Path


ROOT = Path(os.environ.get("APP_ROOT", Path(__file__).resolve().parents[1]))
RAM_SIZE = 256 * 1024
LOW_ALIAS = 0x00800000
HIGH_ALIAS = 0x20000000


def wait_for_file(path: Path, process: subprocess.Popen[bytes], timeout: float = 20) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise AssertionError(f"Renode exited while waiting for {path}")
        if path.is_file() and path.stat().st_size > 0:
            return
        time.sleep(0.02)
    raise AssertionError(f"timed out waiting for {path}")


def run_monitor(output: Path) -> str:
    snapshot = output / "ram.save"
    high_write = output / "high-write.bin"
    low_write = output / "low-write.bin"
    reset = output / "reset.bin"
    power_loss = output / "power-loss.bin"
    before = output / "before.bin"
    cleared = output / "cleared.bin"
    restored = output / "restored.bin"
    restored_again = output / "restored-again.bin"
    boot = ROOT / "renode" / "boot-native-ram.resc"

    home = output / "home"
    (home / ".config" / "renode").mkdir(parents=True)
    (home / ".config" / "renode" / "config").write_text(
        "[general]\nserialization-mode = Reflection\n", encoding="ascii"
    )
    env = os.environ.copy()
    env["HOME"] = str(home)
    env["XDG_CONFIG_HOME"] = str(home / ".config")
    log = output / "renode.log"
    log_stream = log.open("wb")
    process = subprocess.Popen(
        ["renode", "--disable-gui", "--console", "--plain", "--hide-log", str(boot)],
        cwd=ROOT,
        env=env,
        stdin=subprocess.PIPE,
        stdout=log_stream,
        stderr=subprocess.STDOUT,
    )

    def send(command: str) -> None:
        assert process.stdin is not None
        process.stdin.write((command + "\n").encode("utf-8"))
        process.stdin.flush()

    try:
        operations = [
            ("sysbus WriteDoubleWord 0x20000040 0x12345678", None),
            (f"sysbus.ram SaveRam @{high_write}", high_write),
            ("sysbus WriteDoubleWord 0x00800044 0x89abcdef", None),
            (f"sysbus.ram SaveRam @{low_write}", low_write),
            ("machine Reset", None),
            (f"sysbus.ram SaveRam @{reset}", reset),
            ("sysbus.ram ClearForPowerLoss", None),
            (f"sysbus.ram SaveRam @{power_loss}", power_loss),
            ("sysbus.ram FillDeterministically 12345", None),
            (f"sysbus.ram SaveRam @{before}", before),
            (f"Save @{snapshot}", snapshot),
            ("sysbus.ram ZeroAll", None),
            (f"sysbus.ram SaveRam @{cleared}", cleared),
            ("Clear", None),
            (f"Load @{snapshot}", None),
            ("mach set 0", None),
            ("machine PostCreationActions", None),
            (f"sysbus.ram SaveRam @{restored}", restored),
            ("Clear", None),
            (f"Load @{snapshot}", None),
            ("mach set 0", None),
            ("machine PostCreationActions", None),
            (f"sysbus.ram SaveRam @{restored_again}", restored_again),
        ]
        for command, result_path in operations:
            send(command)
            if result_path is not None:
                wait_for_file(result_path, process)
                if result_path == snapshot:
                    previous_size = -1
                    stable = 0
                    while stable < 5:
                        size = snapshot.stat().st_size
                        stable = stable + 1 if size == previous_size else 0
                        previous_size = size
                        time.sleep(0.1)
        send("quit")
        process.wait(timeout=10)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        log_stream.close()
    if process.returncode != 0:
        raise AssertionError(f"Renode exited with {process.returncode}:\n{log.read_text(errors='replace')}")
    return log.read_text(encoding="utf-8", errors="replace")


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verify(output: Path, monitor_output: str) -> None:
    del monitor_output

    high_write = (output / "high-write.bin").read_bytes()
    low_write = (output / "low-write.bin").read_bytes()
    reset = (output / "reset.bin").read_bytes()
    power_loss = (output / "power-loss.bin").read_bytes()
    if high_write[0x40:0x44] != bytes.fromhex("78563412"):
        raise AssertionError("write through high RAM alias was not visible")
    if low_write[0x40:0x48] != bytes.fromhex("78563412efcdab89"):
        raise AssertionError("write through low RAM alias was not coherent")
    if reset[0x40:0x48] != low_write[0x40:0x48]:
        raise AssertionError("ordinary reset did not retain RAM")
    if power_loss[0x40:0x48] != bytes(8):
        raise AssertionError("power loss did not clear RAM")

    before = output / "before.bin"
    cleared = output / "cleared.bin"
    restored = output / "restored.bin"
    restored_again = output / "restored-again.bin"
    for path in (before, cleared, restored, restored_again):
        if path.stat().st_size != RAM_SIZE:
            raise AssertionError(f"{path} has unexpected size {path.stat().st_size}")

    zero_digest = hashlib.sha256(bytes(RAM_SIZE)).hexdigest()
    if digest(cleared) != zero_digest:
        raise AssertionError("ZeroAll did not clear the complete RAM backing")
    if digest(restored) != digest(before):
        raise AssertionError("snapshot restore changed randomized RAM contents")
    if digest(restored_again) != digest(before):
        raise AssertionError("second snapshot restore changed RAM contents")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--keep", action="store_true")
    args = parser.parse_args()

    if args.keep:
        output = Path(tempfile.mkdtemp(prefix="native-power-loss-ram-"))
        monitor_output = run_monitor(output)
        verify(output, monitor_output)
        print(f"PASS: native RAM artifacts retained at {output}")
        return 0

    with tempfile.TemporaryDirectory(prefix="native-power-loss-ram-") as temporary:
        output = Path(temporary)
        monitor_output = run_monitor(output)
        verify(output, monitor_output)
    print("PASS: NativePowerLossRam alias/reset/power-loss/snapshot checks")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
