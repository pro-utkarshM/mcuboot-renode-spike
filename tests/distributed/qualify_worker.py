#!/usr/bin/env python3
"""Compare ARM worker evidence with qualified x86 evidence fail-closed."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

try:
    from .evidence import read_json, validate_envelope
    from .generate_shards import validate_shard
    from .manifest import (
        DistributedProofError,
        canonical_hash,
        hash_declared_inputs,
        load_lineage,
        load_manifest,
        validate_lineages,
        write_immutable_json,
    )
except ImportError:  # pragma: no cover
    from evidence import read_json, validate_envelope  # type: ignore
    from generate_shards import validate_shard  # type: ignore
    from manifest import (  # type: ignore
        DistributedProofError, canonical_hash, hash_declared_inputs,
        load_lineage, load_manifest, validate_lineages,
        write_immutable_json)


DEFAULT_CUTS = (1, 4321, 9000, 10163, 10164, 10165, 10166, 15355, 30695, 30709)
DETERMINISTIC_MATRIX_FIELDS = (
    "cut_point", "operation", "type", "address", "length", "final_image",
    "boots", "state_valid", "result", "flash_hash", "trace_hash",
    "uart_semantic_hash", "mcumgr_hash", "fault_snapshot_hash",
)


def _lineages(directory: Path, manifest: Mapping[str, Any]) -> dict[str, Any]:
    paths = sorted(directory.glob("lineage-*.json")) or sorted(directory.glob("*.json"))
    return validate_lineages([load_lineage(path, manifest) for path in paths], manifest)


def _shards(directory: Path, manifest: Mapping[str, Any], lineages: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for path in sorted(directory.glob("shard-*.json")):
        shard = validate_shard(read_json(path, "qualification shard"), manifest, lineages)
        if shard["shard_id"] in result:
            raise DistributedProofError(
                f"duplicate qualification shard: {shard['shard_id']}")
        result[shard["shard_id"]] = shard
    return result


def _results(
    directory: Path,
    manifest: Mapping[str, Any],
    lineages: Mapping[str, Any],
    shards: Mapping[str, Mapping[str, Any]],
    worker_id: str,
    repetition: int,
) -> dict[int, dict[str, Any]]:
    result: dict[int, dict[str, Any]] = {}
    for path in sorted(directory.rglob("result-*.json")):
        payload = read_json(path, "qualification result")
        shard = shards.get(payload.get("shard_id"))
        if shard is None or shard["worker"]["worker_id"] != worker_id:
            continue
        envelope = validate_envelope(
            payload, manifest, lineages, path.name,
            shard["shard_id"], shard["worker"])
        if envelope["repetition"] != repetition:
            continue
        cut = envelope["cut"]
        if cut in result:
            raise DistributedProofError(
                f"duplicate qualification result for {worker_id} cut {cut}")
        result[cut] = envelope
    return result


def _stable_run(value: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(value)
    result.pop("uart_raw_sha256", None)
    return result


def _stable_evidence(envelope: Mapping[str, Any]) -> dict[str, Any]:
    compact = envelope["compact_evidence"]
    row = compact["matrix_row"]
    return {
        "matrix": {field: row[field] for field in DETERMINISTIC_MATRIX_FIELDS},
        "final_state_verification": _stable_run(
            compact["final_state_verification"]),
        "committed_operation_verification": compact[
            "committed_operation_verification"],
    }


def qualify(
    manifest_path: Path,
    lineages_dir: Path,
    shards_dir: Path,
    reference_results: Path,
    candidate_results: Path,
    reference_worker: str,
    candidate_worker: str,
    repetition: int,
    cuts: Sequence[int],
) -> dict[str, Any]:
    manifest = load_manifest(manifest_path)
    hash_declared_inputs(manifest, manifest_path.parent)
    reference_registration = manifest["qualified_workers"].get(reference_worker)
    candidate_registration = manifest["qualified_workers"].get(candidate_worker)
    if not isinstance(reference_registration, Mapping) or not isinstance(
            candidate_registration, Mapping):
        raise DistributedProofError(
            "both qualification workers must be registered by the manifest")
    if "x86" not in reference_registration["architecture"].lower():
        raise DistributedProofError("qualification reference worker is not x86")
    candidate_arch = candidate_registration["architecture"].lower()
    if "arm64" not in candidate_arch and "aarch64" not in candidate_arch:
        raise DistributedProofError("qualification candidate worker is not ARM64")
    lineages = _lineages(lineages_dir, manifest)
    shards = _shards(shards_dir, manifest, lineages)
    reference = _results(
        reference_results, manifest, lineages, shards,
        reference_worker, repetition)
    candidate = _results(
        candidate_results, manifest, lineages, shards,
        candidate_worker, repetition)
    required = set(cuts)
    missing_reference = sorted(required - set(reference))
    missing_candidate = sorted(required - set(candidate))
    if missing_reference or missing_candidate:
        raise DistributedProofError(
            "qualification evidence is incomplete; "
            f"reference_missing={missing_reference} candidate_missing={missing_candidate}")
    comparisons = []
    types: set[str] = set()
    for cut in cuts:
        expected = _stable_evidence(reference[cut])
        actual = _stable_evidence(candidate[cut])
        if actual != expected:
            raise DistributedProofError(
                f"proof-relevant ARM/x86 divergence at cut {cut}")
        operation_type = actual["matrix"]["type"]
        types.add(operation_type)
        comparisons.append({
            "cut": cut,
            "operation_type": operation_type,
            "evidence_sha256": canonical_hash(actual),
        })
    if not {"program", "erase"}.issubset(types):
        raise DistributedProofError(
            "qualification cuts must include both a program and an erase operation")
    payload: dict[str, Any] = {
        "schema": "ota.distributed-proof.worker-qualification",
        "version": 1,
        "proof_id": manifest["proof_id"],
        "worker_id": candidate_worker,
        "worker": dict(candidate_registration),
        "reference_worker_id": reference_worker,
        "reference_worker": dict(reference_registration),
        "repetition": repetition,
        "cuts": list(cuts),
        "comparisons": comparisons,
        "raw_uart_excluded": True,
        "result": "pass",
    }
    payload["qualification_id"] = canonical_hash(payload)
    return payload


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--lineages-dir", required=True, type=Path)
    parser.add_argument("--shards", required=True, type=Path)
    parser.add_argument("--reference-results", required=True, type=Path)
    parser.add_argument("--candidate-results", required=True, type=Path)
    parser.add_argument("--reference-worker", required=True)
    parser.add_argument("--candidate-worker", required=True)
    parser.add_argument("--repetition", type=int, default=1)
    parser.add_argument("--cuts", default=",".join(map(str, DEFAULT_CUTS)))
    parser.add_argument("--output", required=True, type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        cuts = tuple(int(value) for value in args.cuts.split(",") if value)
        result = qualify(
            args.manifest, args.lineages_dir, args.shards,
            args.reference_results, args.candidate_results,
            args.reference_worker, args.candidate_worker,
            args.repetition, cuts)
        write_immutable_json(args.output, result)
        print(json.dumps({
            "result": "pass",
            "qualification_id": result["qualification_id"],
            "cuts": len(cuts),
        }, sort_keys=True))
        return 0
    except (DistributedProofError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"worker qualification failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
