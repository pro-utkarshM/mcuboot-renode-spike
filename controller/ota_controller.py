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
import tempfile
import time
from pathlib import Path


ROOT = Path(os.environ.get("APP_ROOT", "/workspace/app"))
if not ROOT.exists():
    ROOT = Path(__file__).resolve().parents[1]

FIXTURES = ROOT / "fixtures"
ARTIFACTS = ROOT / "artifacts"
BASELINE_FLASH = ARTIFACTS / "baseline" / "baseline-flash.bin"
EXPECTED_STATE = ARTIFACTS / "baseline" / "expected-state.json"
FLASH_SIZE = 1024 * 1024
BOOT_OFFSET = 0x00000
SLOT0_OFFSET = 0x0C000
SLOT_SIZE = 0x76000
STORAGE_OFFSET = 0x0F8000
OTA_BOOT_TIMEOUT = 240.0


class ControllerError(RuntimeError):
    pass


def require_file(path: Path) -> None:
    if not path.is_file():
        raise ControllerError(f"required file is missing: {path}")


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
        self._renode_home: Path | None = None
        self._runtime_dir: Path | None = None
        self.pty: Path | None = None
        self.flash: Path | None = None
        self.uart: Path | None = None
        self.trace_path: Path | None = None
        self.renode_log: Path | None = None
        self.fault_evidence: Path | None = None
        self.fault_snapshot: Path | None = None
        self.mcumgr_log = output_dir / "mcumgr-commands.log"

    def start(self) -> "RenodeSession":
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self._runtime_dir = Path(tempfile.mkdtemp(prefix="ota-emulator-run-"))
        self.pty = self._runtime_dir / "mcumgr-uart"
        self.flash = self._runtime_dir / "ota-flash.bin"
        self.uart = self._runtime_dir / "uart.log"
        self.trace_path = self._runtime_dir / "flash-operations.log"
        self.renode_log = self._runtime_dir / "renode-process.log"
        self.fault_evidence = self._runtime_dir / "fault-operation.txt"
        self.fault_snapshot = self._runtime_dir / "fault-committed-flash.bin"
        shutil.copyfile(self.source_flash, self.flash)

        args = [
            "renode", "--disable-gui", "--hide-log", "--port", "0",
            str(ROOT / "renode" / "boot.resc"),
            "-e", f'emulation CreateUartPtyTerminal "mcumgr_term" "{self.pty}"',
            "-e", "connector Connect sysbus.uart0 mcumgr_term",
            "-e", f"sysbus.flash LoadFlash @{self.flash}",
            "-e", f"uart0 CreateFileBackend @{self.uart} true",
        ]
        if self.trace:
            args += ["-e", "sysbus.flash BeginTraceFromEnvironment "
                     f"@{self.trace_path}"]
        args += ["-e", "start"]
        env = os.environ.copy()
        env["FAULT_AFTER_OPERATION"] = str(self.fault_after)
        # Renode 1.16.1 can leave its per-user config lock unusable for an
        # immediately following process.  Every proof flow intentionally
        # starts several independent emulator processes, so give each one a
        # private config home rather than sharing ~/.config/renode.
        # Do not use Renode's own /tmp/renode-* namespace: its startup
        # scavenger removes stale entries there, including a freshly created
        # config home.
        self._renode_home = self._runtime_dir / "home"
        env["HOME"] = str(self._renode_home)
        env["XDG_CONFIG_HOME"] = str(self._renode_home / ".config")
        (self._renode_home / ".config" / "renode").mkdir(parents=True)
        self._renode_stream = self.renode_log.open("wb")
        self.process = subprocess.Popen(
            args, stdin=subprocess.DEVNULL, stdout=self._renode_stream,
            stderr=subprocess.STDOUT, env=env,
        )

        def ready() -> bool:
            if self.process is not None and self.process.poll() is not None:
                raise ControllerError(
                    f"Renode exited during startup; see {self.renode_log}")
            return self.pty.exists() and self.uart.exists()

        try:
            wait_for(ready, "Renode UART PTY", 30.0)
        except BaseException:
            # __exit__ is not entered when __enter__ fails.  Stop explicitly
            # so the launch log is still copied to the mounted artifacts.
            self.stop()
            raise
        return self

    def uart_offset(self) -> int:
        try:
            return self.uart.stat().st_size
        except FileNotFoundError:
            return 0

    def read_uart(self, start: int = 0) -> str:
        try:
            return self.uart.read_bytes()[start:].decode("utf-8", errors="ignore")
        except FileNotFoundError:
            return ""

    def wait_marker(self, marker: str, start: int = 0,
                    timeout: float = 30.0) -> None:
        wait_for(lambda: marker in self.read_uart(start),
                 f"UART marker {marker!r}", timeout)

    def run_mcumgr(self, *arguments: str, attempts: int = 5,
                   timeout: float = 90.0) -> str:
        last = ""
        for attempt in range(1, attempts + 1):
            command = [
                "mcumgr", "--conntype", "serial", "--connstring",
                f"dev={self.pty},baud=115200,mtu=256",
            ] + list(arguments)
            attempt_started = time.monotonic()
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
            elapsed = time.monotonic() - attempt_started
            semantic_error = bool(re.search(r"(?m)^Error(?:\s+response)?:", last))
            with self.mcumgr_log.open("a", encoding="utf-8") as stream:
                stream.write(f"$ {' '.join(command)}\n")
                stream.write(last)
                stream.write(
                    f"\nreturncode={rc} semantic_error={semantic_error} "
                    f"attempt={attempt} elapsed_seconds={elapsed:.3f}\n")
            if rc == 0 and not semantic_error:
                return last
            if semantic_error:
                raise ControllerError(
                    "MCUmgr returned an application error despite exit status 0: "
                    f"{' '.join(arguments)}\n{last}")
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
            self.flash: "final-flash.bin",
            self.uart: "uart.log",
            self.trace_path: "flash-operations.log",
            self.renode_log: "renode-process.log",
            self.fault_evidence: "fault-operation.txt",
            self.fault_snapshot: "fault-committed-flash.bin",
        }
        for source, name in copies.items():
            if source.exists():
                shutil.copyfile(source, self.output_dir / name)
        if self._runtime_dir is not None:
            shutil.rmtree(self._runtime_dir, ignore_errors=True)
            self._runtime_dir = None
            self._renode_home = None

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
    baseline = BASELINE_FLASH
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
    EXPECTED_STATE.write_text(
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


def active_image(image_list: str) -> str:
    blocks = re.split(r"(?=^\s*image=\d+\s+slot=\d+\s*$)", image_list,
                      flags=re.MULTILINE)
    for block in blocks:
        if not re.search(r"^\s*image=0\s+slot=0\s*$", block,
                         flags=re.MULTILINE):
            continue
        if not re.search(r"^\s*flags:\s*.*\bactive\b", block,
                         flags=re.MULTILINE | re.IGNORECASE):
            continue
        version = re.search(r"^\s*version:\s*([^\s]+)", block,
                            flags=re.MULTILINE)
        if version is not None and version.group(1).startswith("1.0.0"):
            return "v1"
        if version is not None and version.group(1).startswith("2.0.0"):
            return "v2"
    raise ControllerError("MCUmgr image list has no recognized active primary image")


def stage_update(session: RenodeSession, image: Path) -> str:
    require_file(image)
    session.run_mcumgr("image", "upload", str(image), attempts=8, timeout=180.0)
    image_list = session.image_list()
    image_hash = version_hash(image_list, "2.0.0")
    session.run_mcumgr("image", "test", image_hash, attempts=8, timeout=30.0)
    return image_hash


def reset_and_wait(session: RenodeSession, version: str,
                   timeout: float = OTA_BOOT_TIMEOUT) -> None:
    offset = session.uart_offset()
    session.run_mcumgr("reset", attempts=3, timeout=15.0)
    session.wait_marker(f"FIRMWARE_VERSION={version}", offset, timeout)


def save_final_list(session: RenodeSession, output_dir: Path) -> str:
    text = session.image_list()
    (output_dir / "mcumgr-image-list.txt").write_text(text, encoding="utf-8")
    return text


def baseline_proof(output_dir: Path) -> None:
    baseline = BASELINE_FLASH
    v2 = FIXTURES / "v2-signed.bin"
    require_file(baseline)

    confirm_dir = output_dir / "confirm"
    with RenodeSession(baseline, confirm_dir, trace=True) as session:
        session.wait_marker("FIRMWARE_VERSION=1.0.0")
        image_hash = stage_update(session, v2)
        reset_and_wait(session, "2.0.0")
        wait_for(
            lambda: ("DURABLE_WRITE_SENTINEL=1" in session.read_uart()
                     or "DURABLE_STATE=already-present" in session.read_uart()),
            "durable v2 state before external confirmation", 45.0)
        session.run_mcumgr("image", "confirm", image_hash,
                           attempts=5, timeout=30.0)
        for _ in range(3):
            reset_and_wait(session, "2.0.0")
        save_final_list(session, confirm_dir)

    revert_dir = output_dir / "revert"
    with RenodeSession(baseline, revert_dir, trace=True) as session:
        session.wait_marker("FIRMWARE_VERSION=1.0.0")
        stage_update(session, v2)
        reset_and_wait(session, "2.0.0")
        reset_and_wait(session, "1.0.0")
        save_final_list(session, revert_dir)


def traced_update(source_flash: Path, image: Path, output_dir: Path,
                  fault_after: int = 0, negative_marker: str | None = None) -> None:
    with RenodeSession(source_flash, output_dir, fault_after=fault_after,
                       trace=True) as session:
        session.wait_marker("FIRMWARE_VERSION=1.0.0")
        stage_update(session, image)
        reset_offset = session.uart_offset()

        if fault_after == 0:
            reset_and_wait(session, "2.0.0")
            final_image = "v2"
            if negative_marker is None:
                wait_for(
                    lambda: ("DURABLE_WRITE_SENTINEL=1" in session.read_uart()
                             or "DURABLE_STATE=already-present" in session.read_uart()),
                    "durable v2 state marker", 45.0)
                session.wait_marker("IMAGE_CONFIRMATION=complete", timeout=45.0)
            else:
                session.wait_marker(negative_marker, timeout=45.0)
        else:
            def fault_seen() -> bool:
                try:
                    return f"fault=power-loss after_op={fault_after}" in \
                        session.trace_path.read_text(
                            encoding="utf-8", errors="replace")
                except FileNotFoundError:
                    return False

            fault_before_reset = fault_seen()
            session.run_mcumgr("reset", attempts=3, timeout=15.0)
            wait_for(fault_seen, f"configured power loss after operation {fault_after}",
                     OTA_BOOT_TIMEOUT)

            # Do not query the old application in the small interval between
            # an acknowledged reset command and the reset taking effect. Cuts
            # that fire during the v2 application have one pre-cut v2 marker
            # and require a second, post-cut recovery marker. Upload/swap cuts
            # and faults already consumed before this reset need only one.
            try:
                first_operation_in_range(
                    session.trace_path, STORAGE_OFFSET, FLASH_SIZE)
                application_phase_cut = not fault_before_reset
            except ControllerError:
                application_phase_cut = False
            required_boots = 2 if application_phase_cut else 1

            def recovery_boot_seen() -> bool:
                markers = re.findall(
                    r"^FIRMWARE_VERSION=(?:1\.0\.0|2\.0\.0)\r?$",
                    session.read_uart(reset_offset), flags=re.MULTILINE)
                return len(markers) >= required_boots

            wait_for(recovery_boot_seen, "post-fault application boot",
                     OTA_BOOT_TIMEOUT)
            if negative_marker is not None:
                session.wait_marker(negative_marker, reset_offset,
                                    OTA_BOOT_TIMEOUT)

            # A cut during upload or swap resumes and can converge to v2. A
            # cut after the test image starts but before confirmation correctly
            # reverts to v1. Query the recovered firmware instead of assuming
            # one outcome; the verifier accepts only these two stable states.
            recovered_list = session.image_list()
            final_image = active_image(recovered_list)
            if final_image == "v2":
                if negative_marker is None:
                    wait_for(
                        lambda: ("DURABLE_WRITE_SENTINEL=1" in session.read_uart()
                                 or "DURABLE_STATE=already-present" in
                                 session.read_uart()),
                        "durable v2 state marker", 45.0)
                    session.wait_marker("IMAGE_CONFIRMATION=complete", timeout=45.0)
            elif negative_marker is not None:
                raise ControllerError(
                    "negative firmware reverted before its seeded bug executed")
            else:
                session.wait_marker("PERSISTENT_SETTING=loaded:generation=1",
                                    reset_offset, OTA_BOOT_TIMEOUT)

        final_version = "2.0.0" if final_image == "v2" else "1.0.0"
        # Bounded recovery boots prove that the selected image remains stable.
        for _ in range(2):
            reset_and_wait(session, final_version)
        save_final_list(session, output_dir)


def first_operation_in_range(trace_path: Path, start: int, end: int) -> int:
    require_file(trace_path)
    record = re.compile(
        r"^op=(\d+) type=(program|erase) address=0x([0-9A-Fa-f]+) length=(\d+)$"
    )
    for line in trace_path.read_text(encoding="utf-8").splitlines():
        match = record.fullmatch(line)
        if match is None:
            continue
        address = int(match.group(3), 16)
        if start <= address < end:
            return int(match.group(1))
    raise ControllerError(
        f"trace has no flash operation in range 0x{start:08x}-0x{end:08x}")


def fault_hook_proof(output_dir: Path) -> None:
    """Select the first durable-state program boundary, then cut exactly there."""
    baseline = BASELINE_FLASH
    image = FIXTURES / "v2-auto-confirm-signed.bin"
    target_dir = output_dir / "target-discovery"
    with RenodeSession(baseline, target_dir, trace=True) as session:
        session.wait_marker("FIRMWARE_VERSION=1.0.0")
        stage_update(session, image)
        reset_and_wait(session, "2.0.0")
        session.wait_marker("DURABLE_WRITE_ARMED=1", timeout=30.0)
        session.wait_marker("DURABLE_WRITE_SENTINEL=1", timeout=30.0)
        session.wait_marker("IMAGE_CONFIRMATION=complete", timeout=30.0)
        save_final_list(session, target_dir)

    # Host polling cannot reliably sample between an emulated marker and the
    # following write: simulated time may advance faster than wall time.  The
    # baseline starts with populated v1 settings and neither upload nor MCUboot
    # touches storage_partition, so its first traced operation is the first
    # completed operation of v2's durable NVS update.
    target = first_operation_in_range(
        target_dir / "flash-operations.log", STORAGE_OFFSET, FLASH_SIZE)

    cut_dir = output_dir / "injected-cut"
    with RenodeSession(baseline, cut_dir, fault_after=target, trace=True) as session:
        session.wait_marker("FIRMWARE_VERSION=1.0.0")
        stage_update(session, image)
        reset_offset = session.uart_offset()
        session.run_mcumgr("reset", attempts=3, timeout=15.0)
        session.wait_marker("FIRMWARE_VERSION=2.0.0", reset_offset,
                            OTA_BOOT_TIMEOUT)
        session.wait_marker("DURABLE_WRITE_ARMED=1", reset_offset, 30.0)
        wait_for(lambda: f"fault=power-loss after_op={target}" in
                 session.trace_path.read_text(
                     encoding="utf-8", errors="replace"),
                 "configured power-loss record", 30.0)

        def reset_seen_after_arm() -> bool:
            tail = session.read_uart(reset_offset)
            arm = tail.find("DURABLE_WRITE_ARMED=1")
            return arm >= 0 and tail.find("FIRMWARE_VERSION=1.0.0", arm) >= 0

        wait_for(reset_seen_after_arm, "post-cut v1 reset vector", 60.0)
        tail = session.read_uart(reset_offset)
        arm = tail.find("DURABLE_WRITE_ARMED=1")
        second_boot = tail.find("FIRMWARE_VERSION=1.0.0", arm)
        if "DURABLE_WRITE_SENTINEL=1" in tail[arm:second_boot]:
            raise ControllerError("post-write sentinel executed before power-loss reset")
        second_boot_absolute = reset_offset + second_boot
        session.wait_marker("PERSISTENT_SETTING=loaded:generation=1",
                            second_boot_absolute, 45.0)
        # The cut occurs before v2 confirms itself. MCUboot's correct recovery
        # is therefore the already-confirmed v1 image, not a second v2 test
        # boot. Repeated resets prove that recovery converged.
        for _ in range(2):
            reset_and_wait(session, "1.0.0")
        final_list = save_final_list(session, cut_dir)
        if not re.search(
                r"version:\s*1\.0\.0(?:\+\d+)?[\s\S]*?flags:\s*active confirmed",
                final_list):
            raise ControllerError("post-cut image state is not confirmed v1")

    (output_dir / "fault-hook-summary.json").write_text(
        json.dumps({
            "result": "pass",
            "selected_operation": target,
            "operation_completed_before_reset": True,
            "post_operation_sentinel_before_reset": False,
            "post_fault_image": "v1",
            "volatile_ram_marker_after_reset": 1,
            "persistent_v1_state_retained": True,
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
            traced_update(BASELINE_FLASH, args.image,
                          args.output_dir)
        elif args.command == "cutpoint":
            if args.cut < 1:
                raise ControllerError("cut point must be positive")
            traced_update(BASELINE_FLASH, args.image,
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
            traced_update(BASELINE_FLASH, image,
                          args.output_dir, fault_after=args.cut,
                          negative_marker=marker)
        return 0
    except (ControllerError, OSError) as exc:
        print(f"controller failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
