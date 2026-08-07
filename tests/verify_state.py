#!/usr/bin/env python3
"""Evidence verifier for the Renode/MCUboot OTA power-loss spike.

The verifier deliberately consumes captured evidence; it never starts Renode,
changes a flash image, or writes an artifact unless a caller requests an output
file for parsed JSON.  All checks use only the Python standard library.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sys
import tempfile
import unittest
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable, Optional


FLASH_SIZE = 1024 * 1024
TRACE_RE = re.compile(
    r"^op=(?P<op>[1-9][0-9]*) type=(?P<type>program|erase) "
    r"address=(?P<address>0x[0-9A-Fa-f]+) length=(?P<length>[1-9][0-9]*)$"
)
FAULT_RE = re.compile(r"^fault=power-loss after_op=(?P<op>[1-9][0-9]*)$")
VERSION = {"v1": "1.0.0", "v2": "2.0.0"}
SEMANTIC_UART_PREFIXES = (
    "FIRMWARE_VERSION=",
    "RAM_BOOT_MARKER_RESET=",
    "PERSISTENT_SETTING=",
    "DURABLE_WRITE_ARMED=",
    "DURABLE_WRITE_SENTINEL=",
    "DURABLE_STATE=",
    "IMAGE_CONFIRMATION=",
    "NEGATIVE_",
)
CORE_MATRIX_COLUMNS = (
    "cut_point", "operation", "type", "address", "length", "final_image",
    "boots", "state_valid", "result",
)


class VerificationError(RuntimeError):
    """A captured evidence file does not support the claimed outcome."""


@dataclass(frozen=True)
class Operation:
    op: int
    type: str
    address: int
    length: int


@dataclass(frozen=True)
class Trace:
    operations: list[Operation]
    fault_after_operation: Optional[int]


def fail(message: str) -> None:
    raise VerificationError(message)


def read_text(path: Path, label: str) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        fail(f"cannot read {label} {path}: {exc}")


def semantic_uart_text(log: str) -> str:
    """Remove binary SMP traffic while retaining ordered firmware evidence."""
    markers = [
        line.rstrip("\r")
        for line in log.splitlines()
        if line.rstrip("\r").startswith(SEMANTIC_UART_PREFIXES)
    ]
    return "\n".join(markers) + ("\n" if markers else "")


def parse_trace_text(text: str, source: str) -> Trace:
    operations: list[Operation] = []
    faults: list[int] = []
    for line_number, raw_line in enumerate(text.splitlines(), 1):
        line = raw_line.strip()
        if not line:
            continue
        match = TRACE_RE.fullmatch(line)
        if match:
            operation = Operation(
                op=int(match.group("op")),
                type=match.group("type"),
                address=int(match.group("address"), 16),
                length=int(match.group("length")),
            )
            if operation.op != len(operations) + 1:
                fail(f"{source}:{line_number}: operation numbers must start at 1 and be contiguous")
            if operation.address + operation.length > FLASH_SIZE:
                fail(f"{source}:{line_number}: operation extends past 1 MiB flash")
            operations.append(operation)
            continue
        match = FAULT_RE.fullmatch(line)
        if match:
            faults.append(int(match.group("op")))
            continue
        fail(f"{source}:{line_number}: unrecognized trace record: {raw_line!r}")
    if len(faults) > 1:
        fail(f"{source}: expected at most one power-loss record, found {len(faults)}")
    if faults and (not operations or faults[0] > operations[-1].op):
        fail(f"{source}: fault record refers to an operation that was not traced")
    return Trace(operations=operations, fault_after_operation=faults[0] if faults else None)


def parse_trace_file(path: Path) -> Trace:
    return parse_trace_text(read_text(path, "trace"), str(path))


def trace_json(trace: Trace) -> dict[str, object]:
    return {
        "operations": [asdict(operation) | {"address": f"0x{operation.address:08x}"}
                       for operation in trace.operations],
        "fault": (None if trace.fault_after_operation is None else {
            "type": "power-loss", "after_op": trace.fault_after_operation,
        }),
    }


def require_image_present(flash: bytes, image_path: Path, name: str) -> None:
    try:
        image = image_path.read_bytes()
    except OSError as exc:
        fail(f"cannot read {name} signed-image fixture {image_path}: {exc}")
    if not image:
        fail(f"{name} signed-image fixture {image_path} is empty")
    occurrences = flash.count(image)
    if occurrences != 1:
        fail(f"{name} signed-image fixture bytes must occur exactly once in flash; found {occurrences}")


def require_uart_markers(log: str, expected_final: str, durable_state: str) -> None:
    expected_version = VERSION[expected_final]
    version_matches = list(re.finditer(
        r"^FIRMWARE_VERSION=([0-9]+\.[0-9]+\.[0-9]+)\r?$", log,
        re.MULTILINE))
    version_markers = [match.group(1) for match in version_matches]
    if expected_version not in version_markers:
        fail(f"UART log lacks final firmware version marker FIRMWARE_VERSION={expected_version}")
    ram_markers = re.findall(r"^RAM_BOOT_MARKER_RESET=([0-9]+)$", log, re.MULTILINE)
    if not ram_markers:
        fail("UART log lacks RAM_BOOT_MARKER_RESET marker")
    if any(marker != "1" for marker in ram_markers):
        fail(f"RAM boot marker demonstrates retained volatile state: {ram_markers}")
    if not re.search(r"^PERSISTENT_SETTING=(?:initialized|loaded):generation=1$", log, re.MULTILINE):
        fail("UART log lacks persistent settings generation marker")
    final_boot = next(
        match for match in reversed(version_matches)
        if match.group(1) == expected_version)
    final_boot_log = log[final_boot.start():]
    reloaded = bool(re.search(
        r"^DURABLE_STATE=already-present\r?$", final_boot_log, re.MULTILINE))
    written = bool(re.search(
        r"^DURABLE_WRITE_SENTINEL=1\r?$", final_boot_log, re.MULTILINE))
    if durable_state == "present" and not reloaded:
        fail("final v2 reboot did not reload the required durable state")
    if durable_state == "absent" and (reloaded or written):
        fail("final firmware boot observed durable v2 state when it must be absent")


def require_mcumgr_final(text: str, expected_final: str) -> None:
    """Accept mcumgr's human-readable image-list output without trusting it alone."""
    expected_version = VERSION[expected_final]
    blocks = re.split(r"(?=^\s*image=\d+\s+slot=\d+\s*$)", text, flags=re.MULTILINE)
    for block in blocks:
        if not re.search(r"^\s*image=0\s+slot=0\s*$", block, re.MULTILINE):
            continue
        version = re.search(r"^\s*version:\s*([^\s]+)", block, re.MULTILINE)
        active = re.search(r"^\s*flags:\s*.*\bactive\b", block,
                           re.MULTILINE | re.IGNORECASE)
        if version and active and version.group(1) == expected_version:
            return
    fail(f"mcumgr image list does not show active primary image version {expected_version}")


def verify_run(args: argparse.Namespace) -> dict[str, object]:
    try:
        flash = args.flash.read_bytes()
    except OSError as exc:
        fail(f"cannot read flash image {args.flash}: {exc}")
    if len(flash) != FLASH_SIZE:
        fail(f"flash image must be exactly {FLASH_SIZE} bytes (1 MiB), got {len(flash)}")

    require_image_present(flash, args.v1_image, "v1")
    require_image_present(flash, args.v2_image, "v2")
    # The selected final image must be physically present as a real signed build
    # product, not merely named in UART/MCUmgr output.
    uart_text = read_text(args.uart_log, "UART log")
    mcumgr_text = read_text(args.mcumgr_list, "mcumgr image list")
    require_uart_markers(uart_text, args.expected_final, args.durable_state)
    require_mcumgr_final(mcumgr_text, args.expected_final)

    trace = parse_trace_file(args.trace)
    if args.fault_operation is not None:
        if trace.fault_after_operation != args.fault_operation:
            fail("fault trace does not contain exactly one power-loss record after "
                 f"selected operation {args.fault_operation}")
        if not any(op.op == args.fault_operation for op in trace.operations):
            fail(f"selected fault operation {args.fault_operation} has no completed operation record")
    elif trace.fault_after_operation is not None:
        fail("baseline verification received a power-loss record; supply --fault-operation for a cut run")

    return {
        "result": "pass",
        "final_image": args.expected_final,
        "flash_sha256": hashlib.sha256(flash).hexdigest(),
        "trace_sha256": hashlib.sha256(args.trace.read_bytes()).hexdigest(),
        "uart_raw_sha256": hashlib.sha256(args.uart_log.read_bytes()).hexdigest(),
        "uart_semantic_sha256": hashlib.sha256(
            semantic_uart_text(uart_text).encode("utf-8")).hexdigest(),
        "mcumgr_sha256": hashlib.sha256(args.mcumgr_list.read_bytes()).hexdigest(),
        "operation_count": len(trace.operations),
        "fault_after_operation": trace.fault_after_operation,
        "durable_state": args.durable_state,
    }


def read_matrix(path: Path) -> list[dict[str, str]]:
    try:
        with path.open(newline="", encoding="utf-8") as stream:
            reader = csv.DictReader(stream)
            if reader.fieldnames is None:
                fail(f"matrix {path} has no CSV header")
            missing = [column for column in CORE_MATRIX_COLUMNS if column not in reader.fieldnames]
            if missing:
                fail(f"matrix {path} lacks required columns: {', '.join(missing)}")
            rows = list(reader)
    except OSError as exc:
        fail(f"cannot read matrix {path}: {exc}")
    if not rows:
        fail(f"matrix {path} has no cut-point rows")
    return rows


def validate_matrix_rows(path: Path, rows: list[dict[str, str]],
                         expected_cut_points: int,
                         require_complete: bool) -> dict[str, object]:
    if expected_cut_points < 1:
        fail("expected cut-point count must be positive")
    if len(rows) > expected_cut_points:
        fail(f"matrix {path} has {len(rows)} rows; clean trace has "
             f"{expected_cut_points} operations")
    for index, row in enumerate(rows, 1):
        try:
            cut_point = int(row.get("cut_point", ""))
            operation = int(row.get("operation", ""))
            length = int(row.get("length", ""))
            boots = int(row.get("boots", ""))
            address = int(row.get("address", ""), 16)
        except ValueError:
            fail(f"matrix {path} row {index} has a non-numeric core field")
        if cut_point != index or operation != index:
            fail(f"matrix {path} row {index} is not the contiguous cut point {index}")
        if row.get("type") not in {"program", "erase"}:
            fail(f"matrix {path} row {index} has invalid operation type")
        if length < 1 or boots < 1 or address < 0 or address + length > FLASH_SIZE:
            fail(f"matrix {path} row {index} has invalid length, boots, or address")
        if row.get("result") != "pass" or row.get("state_valid", "").lower() != "true":
            fail(f"matrix {path} row {index} is not a valid passing state")
        if row.get("final_image") not in {"v1", "v2"}:
            fail(f"matrix {path} row {index} has invalid final_image "
                 f"{row.get('final_image')!r}")
    if require_complete and len(rows) != expected_cut_points:
        fail(f"matrix {path} has {len(rows)} rows; expected the complete "
             f"{expected_cut_points}")
    return {
        "result": "pass",
        "rows": len(rows),
        "expected_cut_points": expected_cut_points,
        "complete": len(rows) == expected_cut_points,
    }


def validate_matrix(path: Path, expected_cut_points: int,
                    require_complete: bool) -> dict[str, object]:
    rows = read_matrix(path)
    return validate_matrix_rows(path, rows, expected_cut_points, require_complete)


def compare_matrices(paths: Iterable[Path], hash_columns: list[str],
                     expected_cut_points: int) -> dict[str, object]:
    paths = list(paths)
    if len(paths) < 2:
        fail("compare-matrix requires at least two repetition CSV files")
    matrices = [read_matrix(path) for path in paths]
    for path, rows in zip(paths, matrices):
        validate_matrix_rows(path, rows, expected_cut_points, True)
    fields = list(CORE_MATRIX_COLUMNS) + hash_columns
    if not hash_columns:
        available = set(matrices[0][0])
        hash_columns = sorted(column for column in available if column.endswith("_hash"))
        fields = list(CORE_MATRIX_COLUMNS) + hash_columns
    if not hash_columns:
        fail("matrix comparison requires at least one stable hash column (name it *_hash or pass --hash-column)")
    for path, rows in zip(paths, matrices):
        absent = [column for column in hash_columns if column not in rows[0]]
        if absent:
            fail(f"matrix {path} lacks requested hash columns: {', '.join(absent)}")
    baseline = matrices[0]
    for path, rows in zip(paths[1:], matrices[1:]):
        if len(rows) != len(baseline):
            fail(f"matrix {path} has {len(rows)} rows; expected {len(baseline)}")
        for index, (expected, actual) in enumerate(zip(baseline, rows), 1):
            for field in fields:
                if expected.get(field) != actual.get(field):
                    fail(f"matrix {path} row {index} differs in {field}: "
                         f"{actual.get(field)!r} != {expected.get(field)!r}")
    return {
        "result": "pass",
        "repetitions": len(paths),
        "cut_points": len(baseline),
        "hash_columns": hash_columns,
        "deterministic_outcomes": len(paths) * len(baseline),
    }


def emit(payload: dict[str, object], output: Optional[Path]) -> None:
    encoded = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if output is None:
        sys.stdout.write(encoded)
    else:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(encoded, encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subcommands = parser.add_subparsers(dest="command", required=True)
    trace = subcommands.add_parser("parse-trace", help="validate a Renode flash trace and emit JSON")
    trace.add_argument("--trace", required=True, type=Path)
    trace.add_argument("--output", type=Path, help="optional JSON destination; otherwise stdout")

    run = subcommands.add_parser("verify-run", help="validate one captured OTA run")
    run.add_argument("--flash", required=True, type=Path)
    run.add_argument("--uart-log", required=True, type=Path)
    run.add_argument("--mcumgr-list", required=True, type=Path)
    run.add_argument("--trace", required=True, type=Path)
    run.add_argument("--v1-image", required=True, type=Path)
    run.add_argument("--v2-image", required=True, type=Path)
    run.add_argument("--expected-final", required=True, choices=("v1", "v2"))
    run.add_argument("--fault-operation", type=int, help="selected one-shot cut point; omit for baseline")
    run.add_argument("--durable-state", choices=("present", "absent", "any"), default="any")
    run.add_argument("--output", type=Path, help="optional JSON destination; otherwise stdout")

    validate = subcommands.add_parser("validate-matrix", help="validate matrix coverage")
    validate.add_argument("--matrix", required=True, type=Path)
    validate.add_argument("--expected-cut-points", required=True, type=int)
    validate.add_argument("--complete", action="store_true")
    validate.add_argument("--output", type=Path,
                          help="optional JSON destination; otherwise stdout")

    matrix = subcommands.add_parser("compare-matrix", help="compare repetition CSVs")
    matrix.add_argument("--matrix", action="append", required=True, type=Path,
                        help="one matrix CSV; provide once per repetition")
    matrix.add_argument("--hash-column", action="append", default=[],
                        help="stable hash column (default: every *_hash column)")
    matrix.add_argument("--expected-cut-points", required=True, type=int)
    matrix.add_argument("--output", type=Path, help="optional JSON destination; otherwise stdout")
    subcommands.add_parser("self-test", help="run verifier unit tests")
    return parser


class VerifierTests(unittest.TestCase):
    def test_trace_and_fault_are_parsed(self) -> None:
        trace = parse_trace_text(
            "op=1 type=erase address=0x00001000 length=4096\n"
            "op=2 type=program address=0x00002000 length=4\n"
            "fault=power-loss after_op=2\n", "test")
        self.assertEqual(trace.fault_after_operation, 2)
        self.assertEqual(trace.operations[1].address, 0x2000)

    def test_non_contiguous_trace_is_rejected(self) -> None:
        with self.assertRaises(VerificationError):
            parse_trace_text("op=2 type=program address=0x0 length=1\n", "test")

    def test_semantic_uart_hash_ignores_smp_transport_bytes(self) -> None:
        one = (
            "binary-one\nFIRMWARE_VERSION=2.0.0\r\n"
            "RAM_BOOT_MARKER_RESET=1\r\n"
            "PERSISTENT_SETTING=loaded:generation=1\r\n"
        )
        two = (
            "different-packet\nFIRMWARE_VERSION=2.0.0\n"
            "RAM_BOOT_MARKER_RESET=1\n"
            "PERSISTENT_SETTING=loaded:generation=1\n"
        )
        self.assertEqual(semantic_uart_text(one), semantic_uart_text(two))

    def test_matrix_comparison(self) -> None:
        header = ",".join(CORE_MATRIX_COLUMNS + ("flash_hash",))
        row = "1,1,program,0x0,4,v1,2,true,pass,abc"
        with tempfile.TemporaryDirectory() as directory:
            one, two = Path(directory) / "one.csv", Path(directory) / "two.csv"
            one.write_text(header + "\n" + row + "\n", encoding="utf-8")
            two.write_text(header + "\n" + row + "\n", encoding="utf-8")
            result = compare_matrices([one, two], [], 1)
        self.assertEqual(result["deterministic_outcomes"], 2)

    def test_truncated_matrix_is_rejected_as_complete(self) -> None:
        header = ",".join(CORE_MATRIX_COLUMNS + ("flash_hash",))
        row = "1,1,program,0x0,4,v1,2,true,pass,abc"
        with tempfile.TemporaryDirectory() as directory:
            matrix = Path(directory) / "partial.csv"
            matrix.write_text(header + "\n" + row + "\n", encoding="utf-8")
            with self.assertRaises(VerificationError):
                validate_matrix(matrix, 2, True)

    def test_matrix_hash_mismatch_is_rejected(self) -> None:
        header = ",".join(CORE_MATRIX_COLUMNS + ("flash_hash",))
        with tempfile.TemporaryDirectory() as directory:
            one, two = Path(directory) / "one.csv", Path(directory) / "two.csv"
            one.write_text(
                header + "\n1,1,program,0x0,4,v1,2,true,pass,abc\n",
                encoding="utf-8")
            two.write_text(
                header + "\n1,1,program,0x0,4,v1,2,true,pass,def\n",
                encoding="utf-8")
            with self.assertRaises(VerificationError):
                compare_matrices([one, two], ["flash_hash"], 1)

    def test_complete_v2_run(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            v1, v2 = root / "v1.bin", root / "v2.bin"
            v1.write_bytes(b"signed-v1")
            v2.write_bytes(b"signed-v2")
            flash = bytearray(b"\xff" * FLASH_SIZE)
            flash[0x10000:0x10000 + len(b"signed-v1")] = b"signed-v1"
            flash[0x50000:0x50000 + len(b"signed-v2")] = b"signed-v2"
            flash_path = root / "flash.bin"
            flash_path.write_bytes(flash)
            uart = root / "uart.log"
            uart.write_text(
                "FIRMWARE_VERSION=2.0.0\nRAM_BOOT_MARKER_RESET=1\n"
                "PERSISTENT_SETTING=loaded:generation=1\nDURABLE_WRITE_SENTINEL=1\n"
                "FIRMWARE_VERSION=2.0.0\nRAM_BOOT_MARKER_RESET=1\n"
                "PERSISTENT_SETTING=loaded:generation=1\n"
                "DURABLE_STATE=already-present\n",
                encoding="utf-8")
            mcumgr = root / "mcumgr.txt"
            mcumgr.write_text("image=0 slot=0\nversion: 2.0.0\nflags: active confirmed\n", encoding="utf-8")
            trace = root / "trace.log"
            trace.write_text(
                "op=1 type=program address=0x00050000 length=9\n"
                "fault=power-loss after_op=1\n", encoding="utf-8")
            args = argparse.Namespace(
                flash=flash_path, uart_log=uart, mcumgr_list=mcumgr, trace=trace,
                v1_image=v1, v2_image=v2, expected_final="v2", fault_operation=1,
                durable_state="present")
            self.assertEqual(verify_run(args)["result"], "pass")

    def test_deleted_durable_state_is_rejected_after_reboot(self) -> None:
        log = (
            "FIRMWARE_VERSION=2.0.0\nRAM_BOOT_MARKER_RESET=1\n"
            "PERSISTENT_SETTING=loaded:generation=1\nDURABLE_WRITE_SENTINEL=1\n"
            "FIRMWARE_VERSION=2.0.0\nRAM_BOOT_MARKER_RESET=1\n"
            "PERSISTENT_SETTING=loaded:generation=1\nDURABLE_WRITE_SENTINEL=1\n"
        )
        with self.assertRaises(VerificationError):
            require_uart_markers(log, "v2", "present")


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "self-test":
            suite = unittest.defaultTestLoader.loadTestsFromTestCase(VerifierTests)
            return 0 if unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful() else 1
        if args.command == "parse-trace":
            emit(trace_json(parse_trace_file(args.trace)), args.output)
        elif args.command == "verify-run":
            if args.fault_operation is not None and args.fault_operation < 1:
                fail("--fault-operation must be positive")
            emit(verify_run(args), args.output)
        elif args.command == "validate-matrix":
            emit(validate_matrix(
                args.matrix, args.expected_cut_points, args.complete), args.output)
        elif args.command == "compare-matrix":
            emit(compare_matrices(
                args.matrix, args.hash_column, args.expected_cut_points), args.output)
        return 0
    except VerificationError as exc:
        print(f"verification failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
