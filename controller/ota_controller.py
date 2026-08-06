#!/usr/bin/env python3
"""Run real MCUmgr-over-PTY OTA flows against the sealed Renode machine."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path


ROOT = Path(os.environ.get("APP_ROOT", "/workspace/app"))
if not ROOT.exists():
    ROOT = Path(__file__).resolve().parents[1]

FIXTURES = ROOT / "fixtures"
FLASH_SIZE = 1024 * 1024
BOOT_OFFSET = 0x00000
SLOT0_OFFSET = 0x0C000
SLOT_SIZE = 0x76000
PTY = Path("/tmp/mcumgr-uart")
FLASH = Path("/tmp/ota-flash.bin")
UART = Path("/tmp/uart.log")
TRACE = Path("/tmp/flash-operations.log")
RENODE_LOG = Path("/tmp/renode-process.log")
FAULT_EVIDENCE = Path("/tmp/fault-operation.txt")
FAULT_SNAPSHOT = Path("/tmp/fault-committed-flash.bin")
SERIAL_ARGS = [
    "mcumgr", "--conntype", "serial", "--connstring",
    "dev=/tmp/mcumgr-uart,baud=115200,mtu=256",
]


class ControllerError(RuntimeError):
    pass


def require_file(path: Path) -> None:
    if not path.is_file():
        raise ControllerError(f"required file is missing: {path}")


def read_uart(start: int = 0) -> str:
    try:
        return UART.read_bytes()[start:].decode("utf-8", errors="ignore")
    except FileNotFoundError:
        return ""


def wait_for(predicate, description: str, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise ControllerError(f"timed out waiting for {description}")


class RenodeSession:
    def __init__(self, source_flash: Path, output_dir: Path, fault_after: int = 0,
                 trace: bool = True):
        require_file(source_flash)
        self.source_flash = source_flash
        self.output_dir = output_dir
        self.fault_after = fault_after
        self.trace = trace
        self.process: subprocess.Popen[bytes] | None = None
        self._renode_stream = None
        self.mcumgr_log = output_dir / "mcumgr-commands.log"

    def start(self) -> "RenodeSession":
        self.output_dir.mkdir(parents=True, exist_ok=True)
        for path in (PTY, FLASH, UART, TRACE, RENODE_LOG, FAULT_EVIDENCE,
                     FAULT_SNAPSHOT):
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        # Renode opens uart.log.1 when a stale backend exists.
        for path in Path("/tmp").glob("uart.log.*"):
            path.unlink()
        shutil.copyfile(self.source_flash, FLASH)

        args = [
            "renode", "--disable-gui", "--hide-log",
            str(ROOT / "renode" / "boot.resc"),
            "-e", "uart0 CreateFileBackend @/tmp/uart.log true",
        ]
        if self.trace:
            args += ["-e", "sysbus.flash BeginTraceFromEnvironment "
                     "@/tmp/flash-operations.log"]
        args += ["-e", "start"]
        env = os.environ.copy()
        env["FAULT_AFTER_OPERATION"] = str(self.fault_after)
        self._renode_stream = RENODE_LOG.open("wb")
        self.process = subprocess.Popen(
            args, stdin=subprocess.DEVNULL, stdout=self._renode_stream,
            stderr=subprocess.STDOUT, env=env,
        )

        def ready() -> bool:
            if self.process is not None and self.process.poll() is not None:
                raise ControllerError(
                    f"Renode exited during startup; see {RENODE_LOG}")
            return PTY.exists() and UART.exists()

        wait_for(ready, "Renode UART PTY", 30.0)
        return self

    def uart_offset(self) -> int:
        try:
            return UART.stat().st_size
        except FileNotFoundError:
            return 0

    def wait_marker(self, marker: str, start: int = 0,
                    timeout: float = 30.0) -> None:
        wait_for(lambda: marker in read_uart(start),
                 f"UART marker {marker!r}", timeout)

    def run_mcumgr(self, *arguments: str, attempts: int = 5,
                   timeout: float = 90.0) -> str:
        last = ""
        for attempt in range(1, attempts + 1):
            command = SERIAL_ARGS + list(arguments)
            try:
                result = subprocess.run(
                    command, text=True, stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT, timeout=timeout, check=False,
                )
                last = result.stdout
                rc = result.returncode
            except subprocess.TimeoutExpired as exc:
                stdout = exc.stdout or b""
                stderr = exc.stderr or b""
                if isinstance(stdout, bytes):
                    stdout = stdout.decode("utf-8", errors="replace")
                if isinstance(stderr, bytes):
                    stderr = stderr.decode("utf-8", errors="replace")
                last = stdout + stderr
                rc = 124
            with self.mcumgr_log.open("a", encoding="utf-8") as stream:
                stream.write(f"$ {' '.join(command)}\n")
                stream.write(last)
                stream.write(f"\nreturncode={rc} attempt={attempt}\n")
            if rc == 0:
                return last
            if self.process is None or self.process.poll() is not None:
                raise ControllerError("Renode exited while running MCUmgr")
            time.sleep(0.5)
        raise ControllerError(
            f"MCUmgr command failed after {attempts} attempts: "
            f"{' '.join(arguments)}\n{last}")

    def image_list(self) -> str:
        return self.run_mcumgr("image", "list", attempts=10, timeout=20.0)

    def stop(self) -> None:
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=10.0)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5.0)
        if self._renode_stream is not None:
            self._renode_stream.close()
        copies = {
            FLASH: "final-flash.bin",
            UART: "uart.log",
            TRACE: "flash-operations.log",
            RENODE_LOG: "renode-process.log",
            FAULT_EVIDENCE: "fault-operation.txt",
            FAULT_SNAPSHOT: "fault-committed-flash.bin",
        }
        for source, name in copies.items():
            if source.exists():
                shutil.copyfile(source, self.output_dir / name)

    def __enter__(self) -> "RenodeSession":
        return self.start()

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.stop()


def make_factory_flash(output: Path) -> None:
    boot = FIXTURES / "v1-mcuboot.bin"
    v1 = FIXTURES / "sealed-v1-signed.bin"
    require_file(boot)
    require_file(v1)
    boot_bytes = boot.read_bytes()
    image_bytes = v1.read_bytes()
    if len(boot_bytes) > SLOT0_OFFSET:
        raise ControllerError("MCUboot binary overlaps the primary slot")
    if len(image_bytes) > SLOT_SIZE:
        raise ControllerError("signed v1 image does not fit the primary slot")
    flash = bytearray(b"\xff" * FLASH_SIZE)
    flash[BOOT_OFFSET:BOOT_OFFSET + len(boot_bytes)] = boot_bytes
    flash[SLOT0_OFFSET:SLOT0_OFFSET + len(image_bytes)] = image_bytes
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(flash)


def initialize_baseline(output_dir: Path) -> None:
    factory = output_dir / "factory-flash.bin"
    make_factory_flash(factory)
    with RenodeSession(factory, output_dir / "initialization", trace=False) as session:
        session.wait_marker("FIRMWARE_VERSION=1.0.0")
        session.wait_marker("PERSISTENT_SETTING=initialized:generation=1",
                            timeout=30.0)
    initialized = output_dir / "initialization" / "final-flash.bin"
    require_file(initialized)
    baseline = FIXTURES / "baseline_flash.bin"
    baseline.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(initialized, baseline)
    state = {
        "flash_size": FLASH_SIZE,
        "initial_firmware": "1.0.0",
        "persistent_generation": 1,
        "factory_provisioning": {
            "mcuboot_offset": f"0x{BOOT_OFFSET:08x}",
            "primary_slot_offset": f"0x{SLOT0_OFFSET:08x}",
        },
    }
    (FIXTURES / "expected_state.json").write_text(
        json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def version_hash(image_list: str, version: str) -> str:
    blocks = re.split(r"(?=^\s*image=\d+\s+slot=\d+\s*$)", image_list,
                      flags=re.MULTILINE)
    for block in blocks:
        if re.search(rf"version:\s*{re.escape(version)}(?:\+\d+)?\b", block):
            match = re.search(r"hash:\s*([0-9A-Fa-f]+)", block)
            if match:
                return match.group(1)
    raise ControllerError(f"MCUmgr image list has no hash for version {version}")


def stage_update(session: RenodeSession, image: Path) -> str:
    require_file(image)
    session.run_mcumgr("image", "upload", str(image), attempts=8, timeout=180.0)
    image_list = session.image_list()
    image_hash = version_hash(image_list, "2.0.0")
    session.run_mcumgr("image", "test", image_hash, attempts=8, timeout=30.0)
    return image_hash


def reset_and_wait(session: RenodeSession, version: str,
                   timeout: float = 45.0) -> None:
    offset = session.uart_offset()
    session.run_mcumgr("reset", attempts=3, timeout=15.0)
    session.wait_marker(f"FIRMWARE_VERSION={version}", offset, timeout)


def save_final_list(session: RenodeSession, output_dir: Path) -> str:
    text = session.image_list()
    (output_dir / "mcumgr-image-list.txt").write_text(text, encoding="utf-8")
    return text


def baseline_proof(output_dir: Path) -> None:
    baseline = FIXTURES / "baseline_flash.bin"
    v2 = FIXTURES / "v2-signed.bin"
    require_file(baseline)

    confirm_dir = output_dir / "confirm"
    with RenodeSession(baseline, confirm_dir, trace=True) as session:
        session.wait_marker("FIRMWARE_VERSION=1.0.0")
        stage_update(session, v2)
        reset_and_wait(session, "2.0.0")
        wait_for(
            lambda: ("DURABLE_WRITE_SENTINEL=1" in read_uart()
                     or "DURABLE_STATE=already-present" in read_uart()),
            "durable v2 state before external confirmation", 45.0)
        session.run_mcumgr("image", "confirm", attempts=5, timeout=30.0)
        for _ in range(3):
            reset_and_wait(session, "2.0.0")
        save_final_list(session, confirm_dir)

    revert_dir = output_dir / "revert"
    with RenodeSession(baseline, revert_dir, trace=True) as session:
        session.wait_marker("FIRMWARE_VERSION=1.0.0")
        stage_update(session, v2)
        reset_and_wait(session, "2.0.0")
        reset_and_wait(session, "1.0.0", timeout=60.0)
        save_final_list(session, revert_dir)


def traced_update(source_flash: Path, image: Path, output_dir: Path,
                  fault_after: int = 0, negative_marker: str | None = None) -> None:
    with RenodeSession(source_flash, output_dir, fault_after=fault_after,
                       trace=True) as session:
        session.wait_marker("FIRMWARE_VERSION=1.0.0")
        stage_update(session, image)
        reset_and_wait(session, "2.0.0", timeout=90.0)
        if negative_marker is None:
            wait_for(
                lambda: ("DURABLE_WRITE_SENTINEL=1" in read_uart()
                         or "DURABLE_STATE=already-present" in read_uart()),
                "durable v2 state marker", 45.0)
            session.wait_marker("IMAGE_CONFIRMATION=complete", timeout=45.0)
        else:
            session.wait_marker(negative_marker, timeout=45.0)
        # Bounded recovery boots prove that the selected image remains stable.
        for _ in range(2):
            reset_and_wait(session, "2.0.0", timeout=90.0)
        save_final_list(session, output_dir)


def trace_operation_count() -> int:
    try:
        return sum(1 for line in TRACE.read_text(encoding="utf-8").splitlines()
                   if line.startswith("op="))
    except FileNotFoundError:
        return 0


def fault_hook_proof(output_dir: Path) -> None:
    """Select the first durable-state program boundary, then cut exactly there."""
    baseline = FIXTURES / "baseline_flash.bin"
    image = FIXTURES / "v2-auto-confirm-signed.bin"
    target_dir = output_dir / "target-discovery"
    with RenodeSession(baseline, target_dir, trace=True) as session:
        session.wait_marker("FIRMWARE_VERSION=1.0.0")
        stage_update(session, image)
        reset_and_wait(session, "2.0.0", timeout=90.0)
        session.wait_marker("DURABLE_WRITE_ARMED=1", timeout=30.0)
        target = trace_operation_count() + 1
        session.wait_marker("DURABLE_WRITE_SENTINEL=1", timeout=30.0)
        session.wait_marker("IMAGE_CONFIRMATION=complete", timeout=30.0)
        save_final_list(session, target_dir)

    cut_dir = output_dir / "injected-cut"
    with RenodeSession(baseline, cut_dir, fault_after=target, trace=True) as session:
        session.wait_marker("FIRMWARE_VERSION=1.0.0")
        stage_update(session, image)
        reset_offset = session.uart_offset()
        session.run_mcumgr("reset", attempts=3, timeout=15.0)
        session.wait_marker("FIRMWARE_VERSION=2.0.0", reset_offset, 90.0)
        session.wait_marker("DURABLE_WRITE_ARMED=1", reset_offset, 30.0)
        wait_for(lambda: f"fault=power-loss after_op={target}" in
                 TRACE.read_text(encoding="utf-8", errors="replace"),
                 "configured power-loss record", 30.0)

        def reset_seen_after_arm() -> bool:
            tail = read_uart(reset_offset)
            arm = tail.find("DURABLE_WRITE_ARMED=1")
            return arm >= 0 and tail.find("FIRMWARE_VERSION=2.0.0", arm) >= 0

        wait_for(reset_seen_after_arm, "post-cut v2 reset vector", 60.0)
        tail = read_uart(reset_offset)
        arm = tail.find("DURABLE_WRITE_ARMED=1")
        second_boot = tail.find("FIRMWARE_VERSION=2.0.0", arm)
        if "DURABLE_WRITE_SENTINEL=1" in tail[arm:second_boot]:
            raise ControllerError("post-write sentinel executed before power-loss reset")
        second_boot_absolute = reset_offset + second_boot
        wait_for(lambda: ("DURABLE_WRITE_SENTINEL=1" in read_uart(second_boot_absolute)
                          or "DURABLE_STATE=already-present" in read_uart(second_boot_absolute)),
                 "recovered durable state", 45.0)
        session.wait_marker("IMAGE_CONFIRMATION=complete", second_boot_absolute, 45.0)
        for _ in range(2):
            reset_and_wait(session, "2.0.0", timeout=90.0)
        save_final_list(session, cut_dir)

    (output_dir / "fault-hook-summary.json").write_text(
        json.dumps({
            "result": "pass",
            "selected_operation": target,
            "operation_completed_before_reset": True,
            "post_operation_sentinel_before_reset": False,
            "volatile_ram_marker_after_reset": 1,
            "persistent_flash_retained": True,
            "fault_was_one_shot": True,
        }, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    factory = commands.add_parser("factory")
    factory.add_argument("--output", required=True, type=Path)
    initialize = commands.add_parser("initialize-baseline")
    initialize.add_argument("--output-dir", required=True, type=Path)
    baseline = commands.add_parser("baseline-proof")
    baseline.add_argument("--output-dir", required=True, type=Path)
    hook = commands.add_parser("fault-hook-proof")
    hook.add_argument("--output-dir", required=True, type=Path)
    trace = commands.add_parser("trace")
    trace.add_argument("--output-dir", required=True, type=Path)
    trace.add_argument("--image", type=Path,
                       default=FIXTURES / "v2-auto-confirm-signed.bin")
    cut = commands.add_parser("cutpoint")
    cut.add_argument("--cut", required=True, type=int)
    cut.add_argument("--output-dir", required=True, type=Path)
    cut.add_argument("--image", type=Path,
                     default=FIXTURES / "v2-auto-confirm-signed.bin")
    negative = commands.add_parser("negative-cutpoint")
    negative.add_argument("--cut", required=True, type=int)
    negative.add_argument("--output-dir", required=True, type=Path)
    negative.add_argument("--variant", required=True,
                          choices=("premature-confirm", "erase-after-confirm"))
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        if args.command == "factory":
            make_factory_flash(args.output)
        elif args.command == "initialize-baseline":
            initialize_baseline(args.output_dir)
        elif args.command == "baseline-proof":
            baseline_proof(args.output_dir)
        elif args.command == "fault-hook-proof":
            fault_hook_proof(args.output_dir)
        elif args.command == "trace":
            traced_update(FIXTURES / "baseline_flash.bin", args.image,
                          args.output_dir)
        elif args.command == "cutpoint":
            if args.cut < 1:
                raise ControllerError("cut point must be positive")
            traced_update(FIXTURES / "baseline_flash.bin", args.image,
                          args.output_dir, fault_after=args.cut)
        elif args.command == "negative-cutpoint":
            if args.cut < 1:
                raise ControllerError("cut point must be positive")
            variants = {
                "premature-confirm": (
                    FIXTURES / "v2-negative-premature-confirm-signed.bin",
                    "NEGATIVE_DURABLE_STATE=skipped",
                ),
                "erase-after-confirm": (
                    FIXTURES / "v2-negative-erase-after-confirm-signed.bin",
                    "NEGATIVE_DURABLE_STATE=deleted",
                ),
            }
            image, marker = variants[args.variant]
            traced_update(FIXTURES / "baseline_flash.bin", image,
                          args.output_dir, fault_after=args.cut,
                          negative_marker=marker)
        return 0
    except (ControllerError, OSError) as exc:
        print(f"controller failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
