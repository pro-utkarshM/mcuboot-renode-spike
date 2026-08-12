#!/usr/bin/env python3
"""Generate paired x86/ARM qualification shards outside the proof matrix."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

try:
    from .manifest import (
        DistributedProofError,
        SCHEMA_VERSION,
        SHARD_SCHEMA,
        canonical_hash,
        hash_declared_inputs,
        load_lineage,
        load_manifest,
        validate_lineages,
        write_immutable_json,
    )
except ImportError:  # pragma: no cover
    from manifest import (  # type: ignore
        DistributedProofError, SCHEMA_VERSION, SHARD_SCHEMA, canonical_hash,
        hash_declared_inputs, load_lineage, load_manifest, validate_lineages,
        write_immutable_json)


DEFAULT_CUTS = (1, 4321, 9000, 10163, 10164, 10165, 10166, 15355, 30695, 30709)


def _materialization(
        index: Mapping[str, Any], repetition: int,
        worker: Mapping[str, Any]) -> dict[str, Any]:
    matches = [
        lineage for lineage in index["by_repetition"][repetition]
        if lineage["architecture"] == worker["architecture"]
        and lineage["build_identity"] == worker["build_identity"]
        and lineage["executor_id"] == worker["executor_id"]
    ]
    if len(matches) != 1:
        raise DistributedProofError(
            "qualification requires exactly one materialization for each worker")
    return matches[0]


def generate_qualification_shards(
    manifest: Mapping[str, Any],
    lineages: Sequence[Mapping[str, Any]],
    reference_worker_id: str,
    candidate_worker_id: str,
    repetition: int,
    cuts: Sequence[int],
    output_dir: Path | None = None,
) -> list[dict[str, Any]]:
    if not 1 <= repetition <= manifest["repetitions"]:
        raise DistributedProofError("qualification repetition is outside 1..5")
    if len(set(cuts)) != len(cuts) or not cuts:
        raise DistributedProofError("qualification cuts must be unique and non-empty")
    index = validate_lineages(lineages, manifest)
    workers = []
    for worker_id in (reference_worker_id, candidate_worker_id):
        worker = manifest["qualified_workers"].get(worker_id)
        if not isinstance(worker, Mapping):
            raise DistributedProofError(
                f"qualification worker is not registered: {worker_id}")
        workers.append((worker_id, worker))
    result = []
    operation_types: set[str] = set()
    for worker_id, worker in workers:
        lineage = _materialization(index, repetition, worker)
        cases = []
        for cut_number in cuts:
            if not 1 <= cut_number <= len(lineage["cuts"]):
                raise DistributedProofError(
                    f"qualification cut is outside the matrix: {cut_number}")
            cut = lineage["cuts"][cut_number - 1]
            operation_types.add(cut["type"])
            cases.append({
                "proof_id": manifest["proof_id"],
                "repetition": repetition,
                "cut": cut_number,
                "root_lineage_id": lineage["root_lineage_id"],
                "lineage_id": lineage["lineage_id"],
                "architecture": lineage["architecture"],
                "build_identity": lineage["build_identity"],
                "execution_mode_hash": lineage["execution_mode_hash"],
                "executor_id": lineage["executor_id"],
                "checkpoint_id": cut["checkpoint_id"],
                "checkpoint_path": cut["checkpoint_path"],
                "checkpoint_sha256": cut["checkpoint_sha256"],
                "phase_costs": dict(cut["phase_costs"]),
                "worker_id": worker_id,
                "work_id": (
                    f"qualification:{manifest['proof_id']}:{worker_id}:"
                    f"{repetition}:{cut_number}"),
            })
        payload: dict[str, Any] = {
            "schema": SHARD_SCHEMA,
            "version": SCHEMA_VERSION,
            "proof_id": manifest["proof_id"],
            "qualification_only": True,
            "worker": {
                "worker_id": worker_id,
                "architecture": worker["architecture"],
                "build_identity": worker["build_identity"],
                "executor_id": worker["executor_id"],
                "runtime_image_id": worker["runtime_image_id"],
                "attestation_hashes": dict(worker["attestation_hashes"]),
            },
            "cases": cases,
            "estimated_weighted_cost": 0.0,
            "estimated_seconds": 0.0,
        }
        payload["shard_id"] = canonical_hash(payload)
        result.append(payload)
    if not {"program", "erase"}.issubset(operation_types):
        raise DistributedProofError(
            "qualification cuts must include program and erase operations")
    if output_dir is not None:
        output_dir.mkdir(parents=True, exist_ok=True)
        for shard in result:
            write_immutable_json(
                output_dir / f"shard-{shard['shard_id']}.json", shard)
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--lineages-dir", required=True, type=Path)
    parser.add_argument("--reference-worker", required=True)
    parser.add_argument("--candidate-worker", required=True)
    parser.add_argument("--repetition", type=int, default=1)
    parser.add_argument("--cuts", default=",".join(map(str, DEFAULT_CUTS)))
    parser.add_argument("--output", required=True, type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        manifest = load_manifest(args.manifest)
        hash_declared_inputs(manifest, args.manifest.parent)
        paths = (sorted(args.lineages_dir.glob("lineage-*.json"))
                 or sorted(args.lineages_dir.glob("*.json")))
        lineages = [load_lineage(path, manifest) for path in paths]
        cuts = tuple(int(value) for value in args.cuts.split(",") if value)
        shards = generate_qualification_shards(
            manifest, lineages, args.reference_worker,
            args.candidate_worker, args.repetition, cuts, args.output)
        print(json.dumps({
            "result": "pass",
            "shards": len(shards),
            "cases": sum(len(shard["cases"]) for shard in shards),
            "qualification_only": True,
        }, sort_keys=True))
        return 0
    except (DistributedProofError, OSError, ValueError) as exc:
        print(f"qualification shard generation failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
