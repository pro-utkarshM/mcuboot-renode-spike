#!/usr/bin/env python3
"""Generate deterministic, static weighted-LPT proof shards."""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

try:
    from .manifest import (
        DistributedProofError,
        SHARD_SCHEMA,
        SCHEMA_VERSION,
        canonical_hash,
        hash_declared_inputs,
        lineage_for_case,
        load_lineage,
        load_manifest,
        load_worker_profile,
        validate_lineages,
        validate_manifest,
        validate_worker_profile,
        write_immutable_json,
    )
except ImportError:  # pragma: no cover - supports direct script invocation
    from manifest import (  # type: ignore
        DistributedProofError, SHARD_SCHEMA, SCHEMA_VERSION, canonical_hash,
        hash_declared_inputs, lineage_for_case, load_lineage, load_manifest, load_worker_profile, validate_lineages,
        validate_manifest, validate_worker_profile, write_immutable_json,
    )


def _cost(cut: Mapping[str, Any], profile: Mapping[str, Any]) -> tuple[float, float]:
    phase_costs = cut["phase_costs"]
    phase_rates = profile.get("phase_rates", {})
    seconds = sum(
        float(cost) / float(phase_rates.get(phase, profile["cases_per_sec"]))
        for phase, cost in phase_costs.items()
    )
    return seconds, seconds * float(profile["cost_rate"])


def validate_shard(shard: Mapping[str, Any], manifest: Mapping[str, Any],
                   lineages: Mapping[str, Any]) -> dict[str, Any]:
    if shard.get("schema") != SHARD_SCHEMA or shard.get("version") != SCHEMA_VERSION:
        raise DistributedProofError("unsupported shard schema or version")
    if shard.get("proof_id") != manifest["proof_id"]:
        raise DistributedProofError("shard proof_id does not match manifest")
    shard_id = shard.get("shard_id")
    if not isinstance(shard_id, str) or len(shard_id) != 64:
        raise DistributedProofError("shard_id must be a SHA-256 identity")
    body = dict(shard)
    body.pop("shard_id", None)
    if shard_id != canonical_hash(body):
        raise DistributedProofError("shard_id does not match canonical shard payload")
    worker = shard.get("worker")
    if not isinstance(worker, Mapping):
        raise DistributedProofError("shard worker must be an object")
    for field in ("worker_id", "architecture", "build_identity", "executor_id",
                  "runtime_image_id"):
        if not isinstance(worker.get(field), str) or not worker[field]:
            raise DistributedProofError(f"shard worker {field} must be a non-empty string")
    attestations = worker.get("attestation_hashes")
    if not isinstance(attestations, Mapping):
        raise DistributedProofError("shard worker lacks qualification attestation hashes")
    for field in ("environment_hash", "offline_hash", "unprivileged_hash"):
        value = attestations.get(field)
        if (not isinstance(value, str) or len(value) != 64
                or any(character not in "0123456789abcdef" for character in value)):
            raise DistributedProofError(f"invalid shard worker attestation hash: {field}")
    registered_worker = manifest["qualified_workers"].get(worker.get("worker_id"))
    if not isinstance(registered_worker, Mapping):
        raise DistributedProofError("shard worker is not registered by the proof manifest")
    for field in ("architecture", "build_identity", "executor_id",
                  "runtime_image_id", "attestation_hashes"):
        if worker.get(field) != registered_worker.get(field):
            raise DistributedProofError(f"shard worker {field} does not match manifest")
    if not isinstance(shard.get("cases"), list) or not shard["cases"]:
        raise DistributedProofError("shard must contain at least one case")
    seen: set[tuple[int, int]] = set()
    for case in shard["cases"]:
        if not isinstance(case, Mapping):
            raise DistributedProofError("shard case must be an object")
        identity = (case.get("repetition"), case.get("cut"))
        if identity in seen:
            raise DistributedProofError("shard contains duplicate work identity")
        seen.add(identity)
        repetition, cut_number = identity
        if repetition not in lineages.get("by_repetition", {}):
            raise DistributedProofError("shard case has an unknown repetition")
        lineage = lineage_for_case(lineages, repetition, case.get("lineage_id"))
        if case.get("proof_id") != manifest["proof_id"]:
            raise DistributedProofError("shard case proof_id does not match manifest")
        if case.get("root_lineage_id") != lineage["root_lineage_id"]:
            raise DistributedProofError("shard case root lineage does not match materialization")
        for field in ("architecture", "build_identity", "execution_mode_hash", "executor_id"):
            if case.get(field) != lineage[field]:
                raise DistributedProofError(
                    f"shard case {field} does not match its lineage")
        for field in ("architecture", "build_identity"):
            if worker.get(field) != case.get(field):
                raise DistributedProofError(
                    f"shard worker {field} does not match its cases")
        if case.get("worker_id") != worker.get("worker_id"):
            raise DistributedProofError("shard case worker_id does not match shard")
        if not isinstance(cut_number, int) or not 1 <= cut_number <= len(lineage["cuts"]):
            raise DistributedProofError("shard case cut is outside the lineage")
        for field in ("checkpoint_id", "checkpoint_path", "checkpoint_sha256"):
            if case.get(field) != lineage["cuts"][cut_number - 1][field]:
                raise DistributedProofError(
                    f"shard case {field} does not match lineage")
    return dict(shard)


def generate_shards(
    manifest: Mapping[str, Any],
    lineages: Sequence[Mapping[str, Any]],
    profiles: Sequence[Mapping[str, Any]],
    output_dir: Path | None = None,
    shards_per_worker: int = 4,
    target_shard_seconds: float = 1800.0,
) -> list[dict[str, Any]]:
    manifest = validate_manifest(manifest)
    lineage_index = validate_lineages(lineages, manifest)
    if not profiles:
        raise DistributedProofError("at least one worker profile is required")
    checked_profiles = [validate_worker_profile(profile, manifest) for profile in profiles]
    worker_ids = [profile["worker_id"] for profile in checked_profiles]
    if len(set(worker_ids)) != len(worker_ids):
        raise DistributedProofError("worker_id values must be unique")
    if isinstance(shards_per_worker, bool) or shards_per_worker < 1:
        raise DistributedProofError("shards_per_worker must be positive")
    if (isinstance(target_shard_seconds, bool)
            or not isinstance(target_shard_seconds, (int, float))
            or target_shard_seconds <= 0):
        raise DistributedProofError("target_shard_seconds must be positive")

    logical_cases: list[dict[str, int]] = []
    for repetition in range(1, manifest["repetitions"] + 1):
        cut_count = len(lineage_index["by_repetition"][repetition][0]["cuts"])
        logical_cases.extend(
            {"repetition": repetition, "cut": cut}
            for cut in range(1, cut_count + 1))

    def candidates(case: Mapping[str, int]) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        repetition, cut_number = case["repetition"], case["cut"]
        for profile in checked_profiles:
            compatible = [
                lineage for lineage in lineage_index["by_repetition"][repetition]
                if lineage["architecture"] == profile["architecture"]
                and lineage["build_identity"] == profile["build_identity"]
                and lineage["executor_id"] == profile["executor_id"]
            ]
            if not compatible:
                continue
            options = []
            for lineage in compatible:
                cut = lineage["cuts"][cut_number - 1]
                seconds, weighted = _cost(cut, profile)
                options.append((seconds, weighted, lineage, cut))
            seconds, weighted, lineage, cut = min(
                options, key=lambda item: (item[0], item[1], item[2]["lineage_id"]))
            result.append({
                "profile": profile,
                "seconds": seconds,
                "weighted": weighted,
                "lineage": lineage,
                "cut_spec": cut,
            })
        return result

    candidate_cache: dict[tuple[int, int], list[dict[str, Any]]] = {}
    for case in logical_cases:
        key = (case["repetition"], case["cut"])
        candidate_cache[key] = candidates(case)
        if not candidate_cache[key]:
            raise DistributedProofError(
                f"no qualified worker/materialization for r{key[0]}-c{key[1]}")

    # Each slot is one bounded shard assigned to a physical worker profile.
    # Repeating every profile for the same number of waves lets the unrelated-
    # machine scheduler assign more cases to faster hosts while preserving
    # small, resumable shard files.
    total_min_seconds = sum(
        min(candidate["seconds"] for candidate in candidate_cache[
            (case["repetition"], case["cut"])])
        for case in logical_cases)
    waves = max(
        shards_per_worker,
        math.ceil(total_min_seconds /
                  (target_shard_seconds * len(checked_profiles))),
    )
    slot_profiles = (checked_profiles * waves)[:len(logical_cases)]
    slot_count = len(slot_profiles)
    slots: list[dict[str, Any]] = []
    for index in range(slot_count):
        profile = slot_profiles[index]
        slots.append({"slot": index, "profile": profile, "cases": [], "weighted": 0.0, "seconds": 0.0})

    def sort_key(case: Mapping[str, int]) -> tuple[float, int, int]:
        estimate = max(
            candidate["seconds"] for candidate in candidate_cache[
                (case["repetition"], case["cut"])])
        return (-estimate, int(case["repetition"]), int(case["cut"]))

    for case in sorted(logical_cases, key=sort_key):
        key = (case["repetition"], case["cut"])
        by_worker = {
            candidate["profile"]["worker_id"]: candidate
            for candidate in candidate_cache[key]
        }
        eligible = [slot for slot in slots
                    if slot["profile"]["worker_id"] in by_worker]
        if not eligible:
            raise DistributedProofError("no compatible static shard slot")
        slot = min(
            eligible,
            key=lambda item: (
                item["seconds"] + by_worker[item["profile"]["worker_id"]]["seconds"],
                item["weighted"] + by_worker[item["profile"]["worker_id"]]["weighted"],
                item["slot"],
            ),
        )
        selected = by_worker[slot["profile"]["worker_id"]]
        seconds, weighted = selected["seconds"], selected["weighted"]
        lineage, cut = selected["lineage"], selected["cut_spec"]
        assigned = {
            "proof_id": manifest["proof_id"],
            "repetition": case["repetition"],
            "cut": case["cut"],
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
        }
        assigned["worker_id"] = slot["profile"]["worker_id"]
        assigned["estimated_seconds"] = round(seconds, 9)
        assigned["weighted_cost"] = round(weighted, 9)
        assigned["work_id"] = (
            f"{manifest['proof_id']}:{case['repetition']}:{case['cut']}")
        slot["cases"].append(assigned)
        slot["weighted"] += weighted
        slot["seconds"] += seconds

    shards: list[dict[str, Any]] = []
    for slot in slots:
        if not slot["cases"]:
            continue
        profile = slot["profile"]
        payload: dict[str, Any] = {
            "schema": SHARD_SCHEMA,
            "version": SCHEMA_VERSION,
            "proof_id": manifest["proof_id"],
            "worker": {
                "worker_id": profile["worker_id"],
                "architecture": profile["architecture"],
                "build_identity": profile["build_identity"],
                "executor_id": profile["executor_id"],
                "runtime_image_id": profile["runtime_image_id"],
                "attestation_hashes": dict(profile["attestation_hashes"]),
            },
            "cases": sorted(slot["cases"], key=lambda item: (item["repetition"], item["cut"])),
            "estimated_weighted_cost": round(slot["weighted"], 9),
            "estimated_seconds": round(slot["seconds"], 9),
        }
        payload["shard_id"] = canonical_hash(payload)
        shards.append(payload)
    shards.sort(key=lambda item: item["shard_id"])
    if output_dir is not None:
        output_dir.mkdir(parents=True, exist_ok=True)
        for shard in shards:
            write_immutable_json(output_dir / f"shard-{shard['shard_id']}.json", shard)
    return shards


def _paths_from_args(values: list[Path], directory: Path | None) -> list[Path]:
    paths = list(values)
    if directory is not None:
        paths.extend(sorted(directory.glob("*.json")))
    if not paths:
        raise DistributedProofError("at least one input path is required")
    return paths


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--lineage", action="append", type=Path, default=[])
    parser.add_argument("--lineages-dir", type=Path)
    parser.add_argument("--worker-profile", action="append", type=Path, default=[])
    parser.add_argument("--profiles-dir", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--shards-per-worker", type=int, default=4)
    parser.add_argument("--target-shard-seconds", type=float, default=1800.0)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        manifest = load_manifest(args.manifest)
        hash_declared_inputs(manifest, args.manifest.parent)
        lineages = [load_lineage(path, manifest)
                    for path in _paths_from_args(args.lineage, args.lineages_dir)]
        profiles = [load_worker_profile(path)
                    for path in _paths_from_args(args.worker_profile, args.profiles_dir)]
        shards = generate_shards(
            manifest, lineages, profiles, args.output,
            args.shards_per_worker, args.target_shard_seconds)
        print(json.dumps({"shards": len(shards), "cases": sum(len(s["cases"]) for s in shards)},
                         sort_keys=True))
        return 0
    except DistributedProofError as exc:
        print(f"distributed shard generation failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
