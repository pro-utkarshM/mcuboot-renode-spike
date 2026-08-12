#!/usr/bin/env python3
"""Fail-closed aggregation of an immutable distributed results tree."""

from __future__ import annotations

import argparse
import csv
import json
import os
import secrets
import shutil
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

try:
    from .evidence import MATRIX_COLUMNS, read_json, validate_envelope
    from .generate_shards import validate_shard
    from .manifest import (
        DistributedProofError,
        ImmutableOutputError,
        SUMMARY_SCHEMA,
        SCHEMA_VERSION,
        canonical_hash,
        canonical_json,
        fsync_directory,
        hash_declared_inputs,
        load_lineage,
        load_manifest,
        validate_lineages,
        write_immutable_json,
    )
except ImportError:  # pragma: no cover
    from evidence import MATRIX_COLUMNS, read_json, validate_envelope  # type: ignore
    from generate_shards import validate_shard  # type: ignore
    from manifest import (  # type: ignore
        DistributedProofError, ImmutableOutputError, SUMMARY_SCHEMA, SCHEMA_VERSION,
        canonical_hash, canonical_json, fsync_directory, hash_declared_inputs, load_lineage,
        load_manifest, validate_lineages,
        write_immutable_json,
    )


def _load_shard(path: Path, manifest: Mapping[str, Any],
                lineages: Mapping[str, Any]) -> dict[str, Any]:
    payload = read_json(path, "shard")
    try:
        return validate_shard(payload, manifest, lineages)
    except DistributedProofError as exc:
        raise DistributedProofError(f"invalid shard {path}: {exc}") from exc


def _result_files(root: Path) -> list[Path]:
    # Only canonical result names are candidates. A malformed candidate is
    # still rejected by JSON and identity validation; it is never ignored.
    return sorted(path for path in root.rglob("result-*.json") if path.is_file())


def _write_matrix(path: Path, rows: Sequence[Mapping[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=MATRIX_COLUMNS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
        stream.flush()
        os.fsync(stream.fileno())


def _write_evidence(path: Path, envelopes: Sequence[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as stream:
        for envelope in envelopes:
            stream.write(canonical_json(envelope["compact_evidence"]) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def aggregate_results(
    manifest_path: Path,
    lineage_paths: Sequence[Path],
    shard_paths: Sequence[Path],
    results_root: Path,
    output_dir: Path,
) -> dict[str, Any]:
    if output_dir.exists():
        raise ImmutableOutputError(
            f"refusing to replace an existing aggregation: {output_dir}")
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    staging = output_dir.parent / (
        f".{output_dir.name}.tmp-{os.getpid()}-{secrets.token_hex(6)}")
    staging.mkdir()
    try:
        return _aggregate_results(
            manifest_path, lineage_paths, shard_paths, results_root,
            output_dir, staging)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def _aggregate_results(
    manifest_path: Path,
    lineage_paths: Sequence[Path],
    shard_paths: Sequence[Path],
    results_root: Path,
    output_dir: Path,
    staging: Path,
) -> dict[str, Any]:
    manifest = load_manifest(manifest_path)
    hash_declared_inputs(manifest, manifest_path.parent)
    lineages = validate_lineages([load_lineage(path, manifest) for path in lineage_paths], manifest)
    if not shard_paths:
        raise DistributedProofError("no static shards supplied")
    expected: dict[tuple[int, int], dict[str, Any]] = {}
    shard_ids: set[str] = set()
    shard_cases: dict[tuple[int, int], dict[str, Any]] = {}
    for path in sorted(shard_paths):
        shard = _load_shard(path, manifest, lineages)
        if shard["shard_id"] in shard_ids:
            raise DistributedProofError(f"duplicate shard identity: {shard['shard_id']}")
        shard_ids.add(shard["shard_id"])
        for case in shard["cases"]:
            key = (case["repetition"], case["cut"])
            if key in shard_cases:
                raise DistributedProofError(f"duplicate work identity in shards: r{key[0]}-c{key[1]}")
            shard_cases[key] = shard
            expected[key] = case

    expected_cuts = manifest["canonical_trace"]["operation_count"]
    expected_keys = {
        (repetition, cut)
        for repetition in range(1, manifest["repetitions"] + 1)
        for cut in range(1, expected_cuts + 1)
    }
    if set(expected) != expected_keys:
        missing = sorted(expected_keys - set(expected))
        extra = sorted(set(expected) - expected_keys)
        raise DistributedProofError(f"shard coverage is incomplete; missing={missing} extra={extra}")

    results: dict[tuple[int, int], dict[str, Any]] = {}
    for path in _result_files(results_root):
        try:
            payload = read_json(path, "result")
            envelope = validate_envelope(
                payload,
                manifest,
                lineages,
                path.name,
                (shard_cases.get((payload.get("repetition"), payload.get("cut"))) or {}).get("shard_id"),
                (shard_cases.get((payload.get("repetition"), payload.get("cut"))) or {}).get("worker"),
            )
        except (DistributedProofError, KeyError) as exc:
            raise DistributedProofError(f"invalid result {path}: {exc}") from exc
        key = (envelope["repetition"], envelope["cut"])
        if key not in expected:
            raise DistributedProofError(f"result has unknown work identity: r{key[0]}-c{key[1]}")
        if key in results:
            raise DistributedProofError(f"duplicate result for work identity: r{key[0]}-c{key[1]}")
        if envelope["shard_id"] != shard_cases[key]["shard_id"]:
            raise DistributedProofError("result was published by the wrong immutable shard")
        results[key] = envelope

    if set(results) != expected_keys:
        missing = sorted(expected_keys - set(results))
        raise DistributedProofError(f"result tree is incomplete; missing={missing}")

    matrix_paths: dict[str, str] = {}
    evidence_paths: dict[str, str] = {}
    matrix_results: list[Path] = []
    for repetition in range(1, manifest["repetitions"] + 1):
        ordered = [results[(repetition, cut)]
                   for cut in range(1, expected_cuts + 1)]
        matrix = staging / f"repetition-{repetition}" / "matrix.csv"
        evidence = staging / f"repetition-{repetition}" / "evidence.jsonl"
        _write_matrix(matrix, [item["matrix_row"] for item in ordered])
        _write_evidence(evidence, ordered)
        matrix_paths[str(repetition)] = str(
            output_dir / f"repetition-{repetition}" / "matrix.csv")
        evidence_paths[str(repetition)] = str(
            output_dir / f"repetition-{repetition}" / "evidence.jsonl")
        matrix_results.append(matrix)

    # Import the existing verifier as a library. It remains the acceptance
    # authority for matrix shape and deterministic comparison.
    try:
        from tests.verify_state import compare_matrices, validate_matrix
        validations = [validate_matrix(path, expected_cuts, True) for path in matrix_results]
        comparison = (compare_matrices(
            matrix_results,
            ["flash_hash", "trace_hash", "uart_semantic_hash", "mcumgr_hash",
             "fault_snapshot_hash"],
            expected_cuts,
        )
                      if len(matrix_results) >= 2 else None)
    except (ImportError, OSError, ValueError, RuntimeError) as exc:
        raise DistributedProofError(f"existing verifier gate failed: {exc}") from exc

    summary: dict[str, Any] = {
        "schema": SUMMARY_SCHEMA,
        "version": SCHEMA_VERSION,
        "proof_id": manifest["proof_id"],
        "repetitions": manifest["repetitions"],
        "cut_points": expected_cuts,
        "complete": True,
        "result": "pass",
        "shard_set_sha256": canonical_hash(sorted(shard_ids)),
        "root_lineage_ids": {
            str(repetition): lineages["roots"][repetition]
            for repetition in range(1, manifest["repetitions"] + 1)
        },
        "materialization_ids": sorted(lineages["by_id"]),
        "matrices": matrix_paths,
        "evidence": evidence_paths,
        "verifier": {"validate_matrix": validations, "compare_matrix": comparison},
    }
    summary_path = staging / "distributed-matrix-summary.json"
    if not isinstance(comparison, Mapping):
        raise DistributedProofError(
            "five-repetition determinism comparison did not produce a summary")
    comparison = dict(comparison)
    comparison["proof_id"] = manifest["proof_id"]
    comparison["shard_set_sha256"] = summary["shard_set_sha256"]
    comparison["root_lineage_ids"] = summary["root_lineage_ids"]
    comparison["materialization_ids"] = summary["materialization_ids"]
    summary["verifier"]["compare_matrix"] = comparison
    write_immutable_json(staging / "determinism-summary.json", comparison)
    write_immutable_json(summary_path, summary)
    os.rename(staging, output_dir)
    fsync_directory(output_dir.parent)
    return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--lineage", action="append", required=True, type=Path)
    parser.add_argument("--shards", required=True, type=Path,
                        help="directory containing immutable shard-<sha256>.json files")
    parser.add_argument("--results", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        shard_paths = sorted(args.shards.glob("shard-*.json"))
        summary = aggregate_results(args.manifest, args.lineage, shard_paths,
                                    args.results, args.output)
        print(json.dumps({"result": summary["result"], "repetitions": summary["repetitions"],
                          "cut_points": summary["cut_points"]}, sort_keys=True))
        return 0
    except (DistributedProofError, OSError, json.JSONDecodeError) as exc:
        print(f"distributed aggregation failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
