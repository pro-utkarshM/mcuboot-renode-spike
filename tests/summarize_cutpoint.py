#!/usr/bin/env python3
"""Create compact, machine-readable evidence after a cut run has verified."""

from __future__ import annotations

import argparse
import csv
import io
import json
import sys
from pathlib import Path


COLUMNS = (
    "cut_point", "operation", "type", "address", "length", "final_image",
    "boots", "state_valid", "result", "flash_hash", "trace_hash",
    "uart_semantic_hash", "uart_raw_hash", "mcumgr_hash",
    "fault_snapshot_hash",
)


def load_pass(path: Path, label: str) -> dict[str, object]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("result") != "pass":
        raise SystemExit(f"{label} is not a passing result: {path}")
    return payload


def write_csv(path: Path, row: dict[str, object]) -> None:
    stream = io.StringIO()
    writer = csv.DictWriter(stream, fieldnames=COLUMNS, lineterminator="\n")
    writer.writerow(row)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(stream.getvalue(), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--header", action="store_true")
    parser.add_argument("--cut", type=int)
    parser.add_argument("--operation-type", choices=("program", "erase"))
    parser.add_argument("--address")
    parser.add_argument("--length", type=int)
    parser.add_argument("--final-image", choices=("v1", "v2"))
    parser.add_argument("--boots", type=int)
    parser.add_argument("--verification", type=Path)
    parser.add_argument("--commit-verification", type=Path)
    parser.add_argument("--row-output", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--compact", action="store_true")
    args = parser.parse_args()

    if args.header:
        print(",".join(COLUMNS))
        return 0

    required = {
        "cut": args.cut,
        "operation_type": args.operation_type,
        "address": args.address,
        "length": args.length,
        "final_image": args.final_image,
        "boots": args.boots,
        "verification": args.verification,
        "commit_verification": args.commit_verification,
        "row_output": args.row_output,
        "output": args.output,
    }
    missing = [name for name, value in required.items() if value is None]
    if missing:
        parser.error("missing summary arguments: " + ", ".join(missing))
    if args.cut < 1 or args.length < 1 or args.boots < 1:
        parser.error("cut, length, and boots must be positive")

    verification = load_pass(args.verification, "run verification")
    committed = load_pass(args.commit_verification, "commit verification")
    if verification.get("fault_after_operation") != args.cut:
        raise SystemExit("run verification does not match selected cut")
    if verification.get("final_image") != args.final_image:
        raise SystemExit("run verification does not match final image")
    expected_commit = {
        "operation": args.cut,
        "type": args.operation_type,
        "address": args.address,
        "length": args.length,
    }
    for field, expected in expected_commit.items():
        if committed.get(field) != expected:
            raise SystemExit(
                f"commit verification {field} is {committed.get(field)!r}, "
                f"expected {expected!r}")
    if committed.get("snapshot_matches_committed_bytes") is not True:
        raise SystemExit("commit verification does not prove the completed bytes")

    row = {
        "cut_point": args.cut,
        "operation": args.cut,
        "type": args.operation_type,
        "address": args.address,
        "length": args.length,
        "final_image": args.final_image,
        "boots": args.boots,
        "state_valid": "true",
        "result": "pass",
        "flash_hash": verification["flash_sha256"],
        "trace_hash": verification["trace_sha256"],
        "uart_semantic_hash": verification["uart_semantic_sha256"],
        "uart_raw_hash": verification["uart_raw_sha256"],
        "mcumgr_hash": verification["mcumgr_sha256"],
        "fault_snapshot_hash": committed["snapshot_sha256"],
    }
    evidence = {
        "result": "pass",
        "cut_point": args.cut,
        "matrix_row": row,
        "final_state_verification": verification,
        "committed_operation_verification": committed,
        "bulk_inputs_verified_before_compaction": True,
    }
    write_csv(args.row_output, row)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    if args.compact:
        encoded = json.dumps(evidence, sort_keys=True, separators=(",", ":"))
    else:
        encoded = json.dumps(evidence, indent=2, sort_keys=True)
    args.output.write_text(encoded + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
