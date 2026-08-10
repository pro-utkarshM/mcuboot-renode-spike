#!/usr/bin/env python3
"""File-based deployment operations for static distributed proof workers."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import signal
import sys
import time
from pathlib import Path
from typing import Any, Mapping, Sequence

try:
    from .aggregate import aggregate_results
    from .evidence import (
        atomic_publish_result,
        read_json,
        result_path,
        validate_envelope,
    )
    from .generate_shards import validate_shard
    from .manifest import (
        DistributedProofError,
        canonical_hash,
        hash_declared_inputs,
        load_lineage,
        load_manifest,
        validate_lineages,
        validate_local_environment,
        verify_case_checkpoint,
        write_immutable_json,
    )
    from .run_shard import run_shard
except ImportError:  # pragma: no cover - direct script invocation
    from aggregate import aggregate_results  # type: ignore
    from evidence import (  # type: ignore
        atomic_publish_result, read_json, result_path, validate_envelope)
    from generate_shards import validate_shard  # type: ignore
    from manifest import (  # type: ignore
        DistributedProofError, canonical_hash, hash_declared_inputs, load_lineage,
        load_manifest, validate_lineages, validate_local_environment,
        verify_case_checkpoint, write_immutable_json)
    from run_shard import run_shard  # type: ignore


LAYOUT_DIRECTORIES = (
    "checkpoints",
    "shards/all",
    "shards/ready",
    "transfer/incoming/shards",
    "transfer/incoming/results",
    "results",
    "journals",
    "failures",
    "aggregation",
    "qualification",
    "benchmarks",
    "logs",
    "state/locks",
)


def init_layout(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(root, 0o700)
    for relative in LAYOUT_DIRECTORIES:
        path = root / relative
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(path, 0o700)


def _lineage_paths(directory: Path) -> list[Path]:
    paths = sorted(directory.glob("lineage-*.json"))
    if not paths:
        paths = sorted(directory.glob("*.json"))
    if not paths:
        raise DistributedProofError(f"no lineage manifests found in {directory}")
    return paths


def _context(manifest_path: Path, lineages_dir: Path) -> tuple[dict[str, Any], dict[str, Any], list[Path]]:
    manifest = load_manifest(manifest_path)
    hash_declared_inputs(manifest, manifest_path.parent)
    validate_local_environment(manifest, manifest_path.parent)
    paths = _lineage_paths(lineages_dir)
    lineages = validate_lineages(
        [load_lineage(path, manifest) for path in paths], manifest)
    return manifest, lineages, paths


def _shard_paths(directory: Path) -> list[Path]:
    return sorted(path for path in directory.glob("shard-*.json") if path.is_file())


def _validated_shards(
        directory: Path, manifest: Mapping[str, Any],
        lineages: Mapping[str, Any]) -> list[dict[str, Any]]:
    result = []
    for path in _shard_paths(directory):
        payload = read_json(path, "shard")
        result.append(validate_shard(payload, manifest, lineages))
    return result


def install_shards(
    manifest_path: Path,
    lineages_dir: Path,
    source_dir: Path,
    ready_dir: Path,
    worker_id: str,
) -> dict[str, int]:
    manifest, lineages, _ = _context(manifest_path, lineages_dir)
    sources = _shard_paths(source_dir)
    if not sources:
        raise DistributedProofError(f"no complete shard files found in {source_dir}")
    installed = existing = 0
    for source in sources:
        shard = validate_shard(read_json(source, "incoming shard"), manifest, lineages)
        if shard["worker"]["worker_id"] != worker_id:
            raise DistributedProofError(
                f"incoming shard {shard['shard_id']} belongs to "
                f"{shard['worker']['worker_id']}, not {worker_id}")
        destination = ready_dir / f"shard-{shard['shard_id']}.json"
        if destination.exists():
            current = validate_shard(read_json(destination, "installed shard"), manifest, lineages)
            if current != shard:
                raise DistributedProofError(
                    f"immutable shard collision at {destination}")
            existing += 1
            continue
        write_immutable_json(destination, shard)
        installed += 1
    return {"installed": installed, "existing": existing}


def stage_worker_shards(
    manifest_path: Path,
    lineages_dir: Path,
    source_dir: Path,
    staging_dir: Path,
    worker_id: str,
) -> dict[str, int]:
    manifest, lineages, _ = _context(manifest_path, lineages_dir)
    selected = 0
    for source in _shard_paths(source_dir):
        shard = validate_shard(read_json(source, "owned shard"), manifest, lineages)
        if shard["worker"]["worker_id"] != worker_id:
            continue
        destination = staging_dir / f"shard-{shard['shard_id']}.json"
        if destination.exists():
            current = validate_shard(
                read_json(destination, "staged shard"), manifest, lineages)
            if current != shard:
                raise DistributedProofError(
                    f"immutable staging collision at {destination}")
        else:
            write_immutable_json(destination, shard)
        selected += 1
    if not selected:
        raise DistributedProofError(f"no shards assigned to worker {worker_id}")
    return {"selected": selected}


def _shards_by_id(
        shard_dir: Path, manifest: Mapping[str, Any],
        lineages: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for shard in _validated_shards(shard_dir, manifest, lineages):
        if shard["shard_id"] in result:
            raise DistributedProofError(f"duplicate shard ID: {shard['shard_id']}")
        result[shard["shard_id"]] = shard
    if not result:
        raise DistributedProofError(f"no immutable shards found in {shard_dir}")
    return result


def install_results(
    manifest_path: Path,
    lineages_dir: Path,
    shard_dir: Path,
    source_dir: Path,
    results_root: Path,
) -> dict[str, int]:
    manifest, lineages, _ = _context(manifest_path, lineages_dir)
    shards = _shards_by_id(shard_dir, manifest, lineages)
    sources = sorted(source_dir.rglob("result-*.json"))
    if not sources:
        raise DistributedProofError(f"no complete result envelopes found in {source_dir}")
    installed = existing = 0
    seen: set[tuple[int, int]] = set()
    for source in sources:
        payload = read_json(source, "incoming result")
        shard = shards.get(payload.get("shard_id"))
        if shard is None:
            raise DistributedProofError(
                f"incoming result references unknown shard: {payload.get('shard_id')}")
        envelope = validate_envelope(
            payload, manifest, lineages, source.name,
            shard["shard_id"], shard["worker"])
        key = (envelope["repetition"], envelope["cut"])
        if key in seen:
            raise DistributedProofError(
                f"incoming transfer contains duplicate result r{key[0]}-c{key[1]}")
        seen.add(key)
        destination = result_path(results_root, envelope)
        case_dir = destination.parent
        conflicts = list(case_dir.glob("result-*.json")) if case_dir.exists() else []
        if conflicts:
            if len(conflicts) != 1 or conflicts[0].name != destination.name:
                raise DistributedProofError(
                    f"immutable result collision for r{key[0]}-c{key[1]}")
            current = read_json(conflicts[0], "installed result")
            validate_envelope(
                current, manifest, lineages, conflicts[0].name,
                shard["shard_id"], shard["worker"])
            if current != envelope:
                raise DistributedProofError(
                    f"result content collision for r{key[0]}-c{key[1]}")
            existing += 1
            continue
        atomic_publish_result(destination, envelope)
        installed += 1
    return {"installed": installed, "existing": existing}


def _result_for_case(
    results_root: Path,
    manifest: Mapping[str, Any],
    lineages: Mapping[str, Any],
    shard: Mapping[str, Any],
    case: Mapping[str, Any],
) -> bool:
    directory = (results_root / manifest["proof_id"]
                 / f"r{case['repetition']}" / f"c{case['cut']}")
    candidates = sorted(directory.glob("result-*.json"))
    if not candidates:
        return False
    if len(candidates) != 1:
        raise DistributedProofError(
            f"duplicate committed results for r{case['repetition']}-c{case['cut']}")
    validate_envelope(
        read_json(candidates[0], "committed result"), manifest, lineages,
        candidates[0].name, shard["shard_id"], shard["worker"])
    return True


def status(
    manifest_path: Path,
    lineages_dir: Path,
    shard_dir: Path,
    results_root: Path,
    worker_id: str | None = None,
    state: str = "all",
) -> dict[str, Any]:
    manifest, lineages, _ = _context(manifest_path, lineages_dir)
    shards = _validated_shards(shard_dir, manifest, lineages)
    if worker_id is not None:
        shards = [shard for shard in shards
                  if shard["worker"]["worker_id"] == worker_id]
    total = passed = 0
    complete_shards = 0
    completed_ids: list[str] = []
    pending_ids: list[str] = []
    for shard in shards:
        shard_passed = 0
        for case in shard["cases"]:
            total += 1
            if _result_for_case(results_root, manifest, lineages, shard, case):
                passed += 1
                shard_passed += 1
        if shard_passed == len(shard["cases"]):
            complete_shards += 1
            completed_ids.append(shard["shard_id"])
        else:
            pending_ids.append(shard["shard_id"])
    if state not in {"all", "pending", "completed"}:
        raise DistributedProofError("status state must be all, pending, or completed")
    selected_ids = (completed_ids if state == "completed" else
                    pending_ids if state == "pending" else
                    sorted(completed_ids + pending_ids))
    return {
        "proof_id": manifest["proof_id"],
        "shards": len(shards),
        "complete_shards": complete_shards,
        "cases": total,
        "pass": passed,
        "pending": total - passed,
        "state_filter": state,
        "shard_ids": selected_ids,
    }


def _qualification_gate(
        path: Path | None, manifest: Mapping[str, Any], worker_id: str) -> None:
    if path is None:
        return
    payload = read_json(path, "worker qualification")
    body = dict(payload)
    qualification_id = body.pop("qualification_id", None)
    if (payload.get("schema") != "ota.distributed-proof.worker-qualification"
            or payload.get("version") != 1
            or qualification_id != canonical_hash(body)
            or payload.get("result") != "pass"
            or payload.get("proof_id") != manifest["proof_id"]
            or payload.get("worker_id") != worker_id
            or payload.get("worker") != manifest["qualified_workers"].get(worker_id)):
        raise DistributedProofError(
            "worker qualification is missing, stale, or not passing")


def validate_qualification(
        manifest_path: Path, worker_id: str, qualification_path: Path) -> dict[str, Any]:
    manifest = load_manifest(manifest_path)
    worker = manifest["qualified_workers"].get(worker_id)
    if not isinstance(worker, Mapping):
        raise DistributedProofError("qualification worker is not registered")
    _qualification_gate(qualification_path, manifest, worker_id)
    payload = read_json(qualification_path, "worker qualification")
    return {
        "proof_id": manifest["proof_id"],
        "worker_id": worker_id,
        "qualification_id": payload["qualification_id"],
        "result": "pass",
    }


def worker_loop(
    manifest_path: Path,
    lineages_dir: Path,
    ready_dir: Path,
    results_root: Path,
    journals_dir: Path,
    failures_dir: Path,
    locks_dir: Path,
    worker_id: str,
    slot: int,
    slots: int,
    once: bool,
    poll_seconds: float,
    qualification_path: Path | None,
) -> int:
    if slots < 1 or slot < 0 or slot >= slots:
        raise DistributedProofError("worker slot must satisfy 0 <= slot < slots")
    manifest, lineages, lineage_paths = _context(manifest_path, lineages_dir)
    registration = manifest["qualified_workers"].get(worker_id)
    if not isinstance(registration, Mapping):
        raise DistributedProofError("worker is not registered by the manifest")
    architecture = str(registration["architecture"]).lower()
    arm_worker = "arm64" in architecture or "aarch64" in architecture
    qualification_only = arm_worker and qualification_path is None
    _qualification_gate(qualification_path, manifest, worker_id)
    stopping = False

    def stop(_signum: int, _frame: Any) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    while not stopping:
        progressed = False
        shards = _validated_shards(ready_dir, manifest, lineages)
        for shard in shards:
            if stopping:
                break
            if shard["worker"]["worker_id"] != worker_id:
                raise DistributedProofError(
                    f"ready queue contains shard for another worker: {shard['shard_id']}")
            if qualification_only and shard.get("qualification_only") is not True:
                raise DistributedProofError(
                    "unqualified ARM64 worker encountered a real proof shard")
            if int(shard["shard_id"], 16) % slots != slot:
                continue
            if all(_result_for_case(
                    results_root, manifest, lineages, shard, case)
                   for case in shard["cases"]):
                continue
            locks_dir.mkdir(parents=True, exist_ok=True)
            lock_path = locks_dir / f"{shard['shard_id']}.lock"
            with lock_path.open("a+b") as lock:
                try:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    continue
                progressed = True
                try:
                    run_shard(
                        manifest_path,
                        ready_dir / f"shard-{shard['shard_id']}.json",
                        lineage_paths,
                        results_root,
                        journals_dir / f"shard-{shard['shard_id']}.json",
                        None,
                        failures_dir,
                    )
                except DistributedProofError as exc:
                    print(f"worker shard failed: {exc}", file=sys.stderr)
                    return 2
        if once:
            break
        time.sleep(poll_seconds)
    return 0


def verify_worker_checkpoints(
    manifest_path: Path,
    lineages_dir: Path,
    worker_id: str,
) -> dict[str, Any]:
    manifest, lineages, _ = _context(manifest_path, lineages_dir)
    worker = manifest["qualified_workers"].get(worker_id)
    if not isinstance(worker, Mapping):
        raise DistributedProofError("checkpoint worker is not registered")
    selected = [
        lineage
        for materializations in lineages["by_repetition"].values()
        for lineage in materializations
        if lineage["architecture"] == worker["architecture"]
        and lineage["build_identity"] == worker["build_identity"]
        and lineage["executor_id"] == worker["executor_id"]
    ]
    if len(selected) != manifest["repetitions"]:
        raise DistributedProofError(
            "worker must have exactly one checkpoint materialization per repetition")
    count = 0
    for lineage in selected:
        for cut in lineage["cuts"]:
            verify_case_checkpoint(manifest, manifest_path, lineages, {
                "repetition": lineage["repetition"],
                "cut": cut["cut"],
                "lineage_id": lineage["lineage_id"],
                "checkpoint_id": cut["checkpoint_id"],
                "checkpoint_path": cut["checkpoint_path"],
                "checkpoint_sha256": cut["checkpoint_sha256"],
            })
            count += 1
    return {"proof_id": manifest["proof_id"], "worker_id": worker_id,
            "checkpoint_artifacts": count, "result": "pass"}


def validate_aggregation_summary(
    manifest_path: Path,
    lineages_dir: Path,
    shard_dir: Path,
    summary_path: Path,
) -> dict[str, Any]:
    manifest, lineages, _ = _context(manifest_path, lineages_dir)
    shards = _validated_shards(shard_dir, manifest, lineages)
    summary = read_json(summary_path, "distributed determinism summary")
    expected_shards = canonical_hash(sorted(shard["shard_id"] for shard in shards))
    expected_roots = {
        str(repetition): lineages["roots"][repetition]
        for repetition in range(1, manifest["repetitions"] + 1)
    }
    if (summary.get("proof_id") != manifest["proof_id"]
            or summary.get("result") != "pass"
            or summary.get("shard_set_sha256") != expected_shards
            or summary.get("root_lineage_ids") != expected_roots
            or summary.get("materialization_ids") != sorted(lineages["by_id"])):
        raise DistributedProofError(
            "aggregation summary does not match the current proof, shards, and lineages")
    return {"proof_id": manifest["proof_id"],
            "shard_set_sha256": expected_shards, "result": "pass"}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init-layout")
    init.add_argument("--root", required=True, type=Path)

    def context(command: argparse.ArgumentParser) -> None:
        command.add_argument("--manifest", required=True, type=Path)
        command.add_argument("--lineages-dir", required=True, type=Path)

    install = sub.add_parser("install-shards")
    context(install)
    install.add_argument("--source", required=True, type=Path)
    install.add_argument("--ready", required=True, type=Path)
    install.add_argument("--worker-id", required=True)

    stage = sub.add_parser("stage-shards")
    context(stage)
    stage.add_argument("--source", required=True, type=Path)
    stage.add_argument("--staging", required=True, type=Path)
    stage.add_argument("--worker-id", required=True)

    collect = sub.add_parser("install-results")
    context(collect)
    collect.add_argument("--shards", required=True, type=Path)
    collect.add_argument("--source", required=True, type=Path)
    collect.add_argument("--results", required=True, type=Path)

    show = sub.add_parser("status")
    context(show)
    show.add_argument("--shards", required=True, type=Path)
    show.add_argument("--results", required=True, type=Path)
    show.add_argument("--worker-id")
    show.add_argument("--state", choices=("all", "pending", "completed"),
                      default="all")

    worker = sub.add_parser("worker")
    context(worker)
    worker.add_argument("--ready", required=True, type=Path)
    worker.add_argument("--results", required=True, type=Path)
    worker.add_argument("--journals", required=True, type=Path)
    worker.add_argument("--failures", required=True, type=Path)
    worker.add_argument("--locks", required=True, type=Path)
    worker.add_argument("--worker-id", required=True)
    worker.add_argument("--slot", type=int, default=0)
    worker.add_argument("--slots", type=int, default=1)
    worker.add_argument("--once", action="store_true")
    worker.add_argument("--poll-seconds", type=float, default=15.0)
    worker.add_argument("--qualification", type=Path)

    qualification = sub.add_parser("validate-qualification")
    qualification.add_argument("--manifest", required=True, type=Path)
    qualification.add_argument("--worker-id", required=True)
    qualification.add_argument("--qualification", required=True, type=Path)

    checkpoints = sub.add_parser("verify-checkpoints")
    context(checkpoints)
    checkpoints.add_argument("--worker-id", required=True)

    aggregation_check = sub.add_parser("validate-aggregation")
    context(aggregation_check)
    aggregation_check.add_argument("--shards", required=True, type=Path)
    aggregation_check.add_argument("--summary", required=True, type=Path)

    aggregate = sub.add_parser("aggregate")
    context(aggregate)
    aggregate.add_argument("--shards", required=True, type=Path)
    aggregate.add_argument("--results", required=True, type=Path)
    aggregate.add_argument("--output", required=True, type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "init-layout":
            init_layout(args.root)
            result: Any = {"root": str(args.root), "initialized": True}
        elif args.command == "install-shards":
            result = install_shards(
                args.manifest, args.lineages_dir, args.source,
                args.ready, args.worker_id)
        elif args.command == "install-results":
            result = install_results(
                args.manifest, args.lineages_dir, args.shards,
                args.source, args.results)
        elif args.command == "stage-shards":
            result = stage_worker_shards(
                args.manifest, args.lineages_dir, args.source,
                args.staging, args.worker_id)
        elif args.command == "status":
            result = status(
                args.manifest, args.lineages_dir, args.shards,
                args.results, args.worker_id, args.state)
        elif args.command == "worker":
            return worker_loop(
                args.manifest, args.lineages_dir, args.ready, args.results,
                args.journals, args.failures, args.locks, args.worker_id,
                args.slot, args.slots, args.once, args.poll_seconds,
                args.qualification)
        elif args.command == "validate-qualification":
            result = validate_qualification(
                args.manifest, args.worker_id, args.qualification)
        elif args.command == "verify-checkpoints":
            result = verify_worker_checkpoints(
                args.manifest, args.lineages_dir, args.worker_id)
        elif args.command == "validate-aggregation":
            result = validate_aggregation_summary(
                args.manifest, args.lineages_dir, args.shards, args.summary)
        elif args.command == "aggregate":
            _, _, lineage_paths = _context(args.manifest, args.lineages_dir)
            result = aggregate_results(
                args.manifest, lineage_paths, _shard_paths(args.shards),
                args.results, args.output)
        else:  # pragma: no cover
            raise AssertionError(args.command)
        print(json.dumps(result, sort_keys=True))
        return 0
    except (DistributedProofError, OSError, json.JSONDecodeError) as exc:
        print(f"distributed deployment command failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
