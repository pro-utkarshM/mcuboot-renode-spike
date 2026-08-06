#!/usr/bin/env python3
"""Verify bytes persisted at the exact flash operation where power was cut."""

import argparse
import hashlib
import json
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", required=True, type=Path)
    parser.add_argument("--snapshot", required=True, type=Path)
    parser.add_argument("--expected-operation", required=True, type=int)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    fields = {}
    for line in args.evidence.read_text(encoding="utf-8").splitlines():
        key, value = line.split("=", 1)
        fields[key] = value
    required = {"operation", "type", "address", "length", "before_sha256",
                "after_sha256"}
    if required - fields.keys():
        raise SystemExit("fault evidence is incomplete")
    if int(fields["operation"]) != args.expected_operation:
        raise SystemExit("fault evidence operation does not match selected cut")
    snapshot = args.snapshot.read_bytes()
    if len(snapshot) != 1024 * 1024:
        raise SystemExit("fault snapshot is not exactly 1 MiB")
    address = int(fields["address"], 16)
    length = int(fields["length"])
    committed = snapshot[address:address + length]
    digest = hashlib.sha256(committed).hexdigest()
    if digest != fields["after_sha256"]:
        raise SystemExit("fault snapshot does not contain the completed operation bytes")
    if fields["type"] == "erase" and committed != b"\xff" * length:
        raise SystemExit("completed erase snapshot is not erased")
    if fields["type"] == "program" and fields["before_sha256"] == fields["after_sha256"]:
        raise SystemExit("selected program operation did not change flash bytes")
    payload = {
        "result": "pass",
        "operation": args.expected_operation,
        "type": fields["type"],
        "address": fields["address"],
        "length": length,
        "snapshot_matches_committed_bytes": True,
    }
    args.output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n",
                           encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
