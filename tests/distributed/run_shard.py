#!/usr/bin/env python3
"""Resumable local execution harness for static distributed proof shards."""

from __future__ import annotations

import argparse
import json
import os
import secrets
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

try:
    from .evidence import (
        DistributedProofError,
        atomic_publish_result,
        hash_directory_artifacts,
        make_envelope,
        read_json,
        read_matrix_row,
        result_path,
        validate_envelope,
        validate_worker_attestation,
    )
    from .generate_shards import validate_shard
    from .manifest import (
        JOURNAL_SCHEMA,
        SCHEMA_VERSION,
        canonical_bytes,
        fsync_directory,
        hash_declared_inputs,
        load_lineage,
        load_manifest,
        validate_lineages,
        validate_local_environment,
        verify_case_checkpoint,
    )
except ImportError:  # pragma: no cover
    from evidence import (  # type: ignore
        DistributedProofError, atomic_publish_result, hash_directory_artifacts, make_envelope, read_json,
        read_matrix_row, result_path, validate_envelope, validate_worker_attestation,
    )
    from generate_shards import validate_shard  # type: ignore
    from manifest import (  # type: ignore
        JOURNAL_SCHEMA, SCHEMA_VERSION, canonical_bytes, fsync_directory, hash_declared_inputs,
        load_lineage, load_manifest, validate_lineages, validate_local_environment,
        verify_case_checkpoint,
    )


Executor = Callable[[Path, Mapping[str, Any]], int | None]


def _replace_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.parent / (
        f".{path.name}.tmp-{os.getpid()}-{secrets.token_hex(6)}")
    try:
        with temp.open("xb") as stream:
            stream.write(canonical_bytes(payload) + b"\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
        fsync_directory(path.parent)
    finally:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass


def _case_key(case: Mapping[str, Any]) -> str:
    return f"r{case['repetition']}-c{case['cut']}"


def _new_journal(shard: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema": JOURNAL_SCHEMA,
        "version": SCHEMA_VERSION,
        "proof_id": shard["proof_id"],
        "shard_id": shard["shard_id"],
        "cases": {
            _case_key(case): {"status": "PENDING", "attempts": 0}
            for case in shard["cases"]
        },
    }


def _load_journal(path: Path, shard: Mapping[str, Any]) -> dict[str, Any]:
    if not path.exists():
        return _new_journal(shard)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise DistributedProofError(f"cannot read shard journal {path}: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("schema") != JOURNAL_SCHEMA:
        raise DistributedProofError("invalid shard journal schema")
    if payload.get("proof_id") != shard["proof_id"] or payload.get("shard_id") != shard["shard_id"]:
        raise DistributedProofError("shard journal identity mismatch")
    expected = {_case_key(case) for case in shard["cases"]}
    if set(payload.get("cases", {})) != expected:
        raise DistributedProofError("shard journal case set does not match immutable shard")
    return payload


def _command_args(template: str | Sequence[str], case_dir: Path, case: Mapping[str, Any]) -> list[str]:
    values = {
        "case_dir": str(case_dir),
        "proof_id": case["proof_id"],
        "repetition": str(case["repetition"]),
        "cut": str(case["cut"]),
        "root_lineage_id": case["root_lineage_id"],
        "lineage_id": case["lineage_id"],
        "checkpoint_id": case["checkpoint_id"],
        "checkpoint_file": case["checkpoint_file"],
    }
    if isinstance(template, str):
        return shlex.split(template.format(**values))
    return [part.format(**values) for part in template]


def _run_executor(
    executor: str | Sequence[str] | Executor | None,
    case_dir: Path,
    case: Mapping[str, Any],
    manifest: Mapping[str, Any],
) -> None:
    executor_id = case["executor_id"]
    profile = manifest["executors"].get(executor_id)
    if not isinstance(profile, Mapping):
        raise DistributedProofError("case references an unknown executor profile")
    configured = profile["argv"]
    if callable(executor):
        if configured != ["test-only-callable"]:
            raise DistributedProofError(
                "callable executors are allowed only by an explicit test manifest")
        status = executor(case_dir, case)
        if status not in (None, 0):
            raise DistributedProofError(f"executor callback returned status {status}")
        return
    supplied = (configured if executor is None else
                shlex.split(executor) if isinstance(executor, str) else list(executor))
    if supplied != configured:
        raise DistributedProofError("executor command does not match the immutable proof manifest")
    args = _command_args(configured, case_dir, case)
    if not args:
        raise DistributedProofError("executor command template is empty")
    environment = os.environ.copy()
    environment.update({
        "DISTRIBUTED_PROOF_ID": manifest["proof_id"],
        "DISTRIBUTED_REPETITION": str(case["repetition"]),
        "DISTRIBUTED_CUT": str(case["cut"]),
        "DISTRIBUTED_CASE_DIR": str(case_dir),
        "DISTRIBUTED_ROOT_LINEAGE_ID": case["root_lineage_id"],
        "DISTRIBUTED_LINEAGE_ID": case["lineage_id"],
        "DISTRIBUTED_CHECKPOINT_ID": case["checkpoint_id"],
        "DISTRIBUTED_CHECKPOINT_FILE": case["checkpoint_file"],
        "DISTRIBUTED_EXECUTOR_ID": executor_id,
    })
    log = case_dir / "executor.log"
    with log.open("wb") as stream:
        completed = subprocess.run(args, cwd=case_dir, env=environment,
                                   stdout=stream, stderr=subprocess.STDOUT, check=False)
    if completed.returncode != 0:
        raise DistributedProofError(f"executor exited with status {completed.returncode}")


def _diagnostic_copy(stage: Path, root: Path, key: str, attempt: int) -> Path:
    target = root / key / f"attempt-{attempt}"
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(stage, target)
    return target


def _existing_pass(
    case: Mapping[str, Any], manifest: Mapping[str, Any],
    lineages: Mapping[str, Any], results_root: Path, shard_id: str,
    worker: Mapping[str, Any],
) -> tuple[str, Path] | None:
    directory = (results_root / manifest["proof_id"]
                 / f"r{case['repetition']}" / f"c{case['cut']}")
    candidates = sorted(directory.glob("result-*.json"))
    if not candidates:
        return None
    if len(candidates) != 1:
        raise DistributedProofError(
            f"multiple immutable results exist for {_case_key(case)}")
    result = candidates[0]
    try:
        payload = read_json(result, "published result")
        validate_envelope(
            payload, manifest, lineages, result.name, shard_id,
            worker,
        )
    except DistributedProofError as exc:
        raise DistributedProofError(
            f"invalid immutable result for {_case_key(case)}: {exc}") from exc
    if payload["repetition"] != case["repetition"] or payload["cut"] != case["cut"]:
        raise DistributedProofError("immutable result work identity mismatch")
    return payload["result_id"], result


def run_shard(
    manifest_path: Path,
    shard_path: Path,
    lineage_paths: Sequence[Path],
    results_root: Path,
    journal_path: Path,
    executor: str | Sequence[str] | Executor | None,
    diagnostics_root: Path | None = None,
) -> dict[str, Any]:
    manifest = load_manifest(manifest_path)
    hash_declared_inputs(manifest, manifest_path.parent)
    validate_local_environment(manifest, manifest_path.parent)
    lineages = validate_lineages([load_lineage(path, manifest) for path in lineage_paths], manifest)
    shard = json.loads(shard_path.read_text(encoding="utf-8"))
    if not isinstance(shard, dict):
        raise DistributedProofError("shard JSON must be an object")
    validate_shard(shard, manifest, lineages)
    journal = _load_journal(journal_path, shard)
    diagnostics = diagnostics_root or journal_path.parent / "diagnostics"
    failures: list[str] = []

    for case in shard["cases"]:
        key = _case_key(case)
        state = journal["cases"][key]
        existing = _existing_pass(
            case, manifest, lineages, results_root,
            shard["shard_id"], shard["worker"])
        if existing is not None:
            result_id, path = existing
            state.update({"status": "PASS", "result_id": result_id,
                          "result_path": str(path)})
            _replace_json(journal_path, journal)
            continue
        # RUNNING is intentionally treated as stale on every invocation. A
        # worker process can have died without leaving a reliable heartbeat.
        state.update({"status": "RUNNING", "attempts": int(state.get("attempts", 0)) + 1})
        _replace_json(journal_path, journal)
        stage = Path(tempfile.mkdtemp(prefix=f"distributed-{key}-"))
        try:
            execution_case = dict(case)
            execution_case["checkpoint_file"] = str(verify_case_checkpoint(
                manifest, manifest_path, lineages, case))
            _run_executor(executor, stage, execution_case, manifest)
            row = read_matrix_row(stage / "row.csv", case["cut"])
            compact = read_json(stage / "evidence.json", "compact evidence")
            attestation = read_json(stage / "worker-attestation.json", "worker attestation")
            validate_worker_attestation(attestation, manifest, case, shard["worker"])
            timing_path = stage / "timing.json"
            timing = read_json(timing_path, "timing") if timing_path.exists() else {"wall_seconds": 0.0}
            raw_artifacts = hash_directory_artifacts(stage)
            envelope = make_envelope(
                manifest, lineages, case, compact, shard["worker"], attestation,
                raw_artifacts, timing, shard["shard_id"],
            )
            envelope["matrix_row"] = row
            # make_envelope already validates and hashes the same row; retain
            # this check to make a changed executor row impossible to publish.
            validate_envelope(
                envelope, manifest, lineages,
                expected_shard_id=shard["shard_id"],
                expected_worker=shard["worker"],
            )
            destination = result_path(results_root, envelope)
            atomic_publish_result(destination, envelope)
            state.update({"status": "PASS", "result_id": envelope["result_id"],
                          "result_path": str(destination)})
            _replace_json(journal_path, journal)
        except Exception as exc:
            diagnostic = _diagnostic_copy(stage, diagnostics, key, state["attempts"])
            state.update({"status": "FAIL", "error": str(exc), "diagnostic_dir": str(diagnostic)})
            _replace_json(journal_path, journal)
            failures.append(f"{key}: {exc}")
        finally:
            shutil.rmtree(stage, ignore_errors=True)

    if failures:
        raise DistributedProofError("shard execution failed: " + "; ".join(failures))
    return journal


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--shard", required=True, type=Path)
    parser.add_argument("--lineage", action="append", required=True, type=Path)
    parser.add_argument("--results", required=True, type=Path)
    parser.add_argument("--journal", required=True, type=Path)
    parser.add_argument("--diagnostics", type=Path)
    parser.add_argument(
        "--executor",
        help=("optional command template override; it must exactly match the manifest "
              "and may use {case_dir}, {proof_id}, {repetition}, {cut}, "
              "{root_lineage_id}, {lineage_id}, {checkpoint_id}, {checkpoint_file}"),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        journal = run_shard(args.manifest, args.shard, args.lineage, args.results,
                            args.journal, args.executor, args.diagnostics)
        print(json.dumps({"statuses": {key: state["status"] for key, state in journal["cases"].items()},
                          "shard_id": journal["shard_id"]}, sort_keys=True))
        return 0
    except (DistributedProofError, OSError, json.JSONDecodeError) as exc:
        print(f"distributed shard execution failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
