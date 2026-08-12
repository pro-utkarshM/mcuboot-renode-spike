#!/usr/bin/env python3
"""Validate current cut-point evidence and publish immutable result envelopes."""

from __future__ import annotations

import csv
import json
import os
import secrets
from pathlib import Path
from typing import Any, Mapping

try:
    from .manifest import (
        ATTESTATION_SCHEMA,
        DistributedProofError,
        ImmutableOutputError,
        RESULT_SCHEMA,
        SCHEMA_VERSION,
        canonical_bytes,
        canonical_hash,
        fsync_directory,
        lineage_for_case,
        sha256_file,
        validate_execution_mode,
        validate_manifest,
    )
except ImportError:  # pragma: no cover
    from manifest import (  # type: ignore
        ATTESTATION_SCHEMA, DistributedProofError, ImmutableOutputError,
        RESULT_SCHEMA, SCHEMA_VERSION, canonical_bytes, canonical_hash,
        fsync_directory, lineage_for_case, sha256_file, validate_execution_mode,
        validate_manifest,
    )

try:
    from tests.summarize_cutpoint import COLUMNS as MATRIX_COLUMNS
except ImportError:  # pragma: no cover
    MATRIX_COLUMNS = (
        "cut_point", "operation", "type", "address", "length", "final_image",
        "boots", "state_valid", "result", "flash_hash", "trace_hash",
        "uart_semantic_hash", "uart_raw_hash", "mcumgr_hash", "fault_snapshot_hash",
    )

REQUIRED_RAW_ARTIFACTS = {
    "final-flash.bin": "flash_sha256",
    "flash-operations.log": "trace_sha256",
    "uart.log": "uart_raw_sha256",
    "mcumgr-image-list.txt": "mcumgr_sha256",
    "fault-committed-flash.bin": "snapshot_sha256",
}
REQUIRED_LOCAL_EVIDENCE = {
    "row.csv", "evidence.json", "verification.json",
    "commit-verification.json", "fault-operation.txt",
}


def _hash(value: Any, field: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
        raise DistributedProofError(f"{field} must be a lowercase SHA-256 digest")
    return value


def _object(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise DistributedProofError(f"{field} must be an object")
    return value


def validate_matrix_row(row: Mapping[str, Any], expected_cut: int | None = None) -> dict[str, str]:
    missing = [column for column in MATRIX_COLUMNS if column not in row]
    if missing:
        raise DistributedProofError("matrix row lacks required columns: " + ", ".join(missing))
    result = {column: str(row[column]) for column in MATRIX_COLUMNS}
    try:
        cut = int(result["cut_point"])
        operation = int(result["operation"])
        length = int(result["length"])
        int(result["boots"])
        address = int(result["address"], 16)
    except ValueError as exc:
        raise DistributedProofError("matrix row has a non-numeric core field") from exc
    if expected_cut is not None and cut != expected_cut:
        raise DistributedProofError("matrix row cut does not match result cut")
    if (operation != cut or cut < 1 or length < 1 or int(result["boots"]) < 1
            or address < 0 or address + length > 1024 * 1024):
        raise DistributedProofError("matrix row has invalid cut, operation, length, or boots")
    if result["type"] not in {"program", "erase"} or result["final_image"] not in {"v1", "v2"}:
        raise DistributedProofError("matrix row has invalid operation type or final image")
    if result["state_valid"].lower() != "true" or result["result"] != "pass":
        raise DistributedProofError("matrix row is not a passing state")
    for column in MATRIX_COLUMNS:
        if column.endswith("hash"):
            _hash(result[column], f"matrix row {column}")
    return result


def validate_compact_evidence(evidence: Mapping[str, Any], expected_cut: int | None = None) -> dict[str, Any]:
    """Validate the exact compact shape emitted by summarize_cutpoint.py."""
    if not isinstance(evidence, Mapping) or evidence.get("result") != "pass":
        raise DistributedProofError("compact evidence is not a passing result")
    if evidence.get("bulk_inputs_verified_before_compaction") is not True:
        raise DistributedProofError("compact evidence lacks pre-compaction input verification")
    cut = evidence.get("cut_point")
    if not isinstance(cut, int) or cut < 1 or (expected_cut is not None and cut != expected_cut):
        raise DistributedProofError("compact evidence has an invalid cut_point")
    row = validate_matrix_row(_object(evidence.get("matrix_row"), "matrix_row"), cut)
    run = dict(_object(evidence.get("final_state_verification"), "final_state_verification"))
    commit = dict(_object(evidence.get("committed_operation_verification"), "committed_operation_verification"))
    if run.get("result") != "pass" or run.get("fault_after_operation") != cut:
        raise DistributedProofError("local final-state verifier evidence did not pass this cut")
    if commit.get("result") != "pass" or commit.get("operation") != cut:
        raise DistributedProofError("local commit verifier evidence did not pass this cut")
    if commit.get("snapshot_matches_committed_bytes") is not True:
        raise DistributedProofError("commit verifier did not prove committed snapshot bytes")
    if run.get("final_image") != row["final_image"]:
        raise DistributedProofError("matrix row and final-state verifier disagree")
    for field in ("type", "length"):
        if str(commit.get(field)) != row[field]:
            raise DistributedProofError(f"commit verifier and matrix row disagree in {field}")
    if str(commit.get("address")) != row["address"]:
        raise DistributedProofError("commit verifier and matrix row disagree in address")
    for field in ("flash_sha256", "trace_sha256", "uart_raw_sha256", "uart_semantic_sha256", "mcumgr_sha256"):
        _hash(run.get(field), f"final_state_verification.{field}")
    _hash(commit.get("snapshot_sha256"), "committed_operation_verification.snapshot_sha256")
    for field in ("before_sha256", "after_sha256"):
        _hash(commit.get(field), f"committed_operation_verification.{field}")
    expected_hashes = {
        "flash_hash": run["flash_sha256"],
        "trace_hash": run["trace_sha256"],
        "uart_raw_hash": run["uart_raw_sha256"],
        "uart_semantic_hash": run["uart_semantic_sha256"],
        "mcumgr_hash": run["mcumgr_sha256"],
        "fault_snapshot_hash": commit["snapshot_sha256"],
    }
    for field, expected in expected_hashes.items():
        if row[field] != expected:
            raise DistributedProofError(
                f"matrix row {field} does not match locally verified evidence")
    normalized = dict(evidence)
    normalized["matrix_row"] = row
    normalized["final_state_verification"] = run
    normalized["committed_operation_verification"] = commit
    return normalized


def read_matrix_row(path: Path, expected_cut: int | None = None) -> dict[str, str]:
    try:
        with path.open(newline="", encoding="utf-8") as stream:
            rows = list(csv.DictReader(stream))
    except (OSError, UnicodeError, csv.Error) as exc:
        raise DistributedProofError(f"cannot read matrix row {path}: {exc}") from exc
    if len(rows) != 1:
        raise DistributedProofError(f"matrix row must contain exactly one row: {path}")
    return validate_matrix_row(rows[0], expected_cut)


def read_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise DistributedProofError(f"cannot read {label} {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise DistributedProofError(f"{label} must be a JSON object: {path}")
    return value


def validate_worker_attestation(
    attestation: Mapping[str, Any], manifest: Mapping[str, Any], case: Mapping[str, Any],
    worker: Mapping[str, Any],
) -> dict[str, Any]:
    if attestation.get("schema") != ATTESTATION_SCHEMA or attestation.get("version") != SCHEMA_VERSION:
        raise DistributedProofError("unsupported worker attestation schema or version")
    for field in ("environment_hash", "offline_hash", "unprivileged_hash"):
        _hash(attestation.get(field), f"worker attestation {field}")
        if attestation.get(field) != worker.get("attestation_hashes", {}).get(field):
            raise DistributedProofError(f"worker attestation {field} is not the qualified value")
    if attestation.get("proof_id") != manifest["proof_id"]:
        raise DistributedProofError("worker attestation proof_id mismatch")
    if attestation.get("worker_id") != worker.get("worker_id"):
        raise DistributedProofError("worker attestation worker_id mismatch")
    for field in ("repetition", "cut", "root_lineage_id", "lineage_id",
                  "checkpoint_id", "checkpoint_path", "checkpoint_sha256",
                  "execution_mode_hash"):
        if attestation.get(field) != case.get(field):
            raise DistributedProofError(f"worker attestation {field} mismatch")
    for field in ("architecture", "build_identity", "executor_id",
                  "runtime_image_id"):
        if attestation.get(field) != worker.get(field):
            raise DistributedProofError(f"worker attestation {field} mismatch")
    validate_execution_mode(manifest, attestation["execution_mode_hash"])
    return dict(attestation)


def result_filename(envelope: Mapping[str, Any]) -> str:
    return f"result-r{envelope['repetition']}-c{envelope['cut']}-{envelope['result_id']}.json"


def result_path(results_root: Path, envelope: Mapping[str, Any]) -> Path:
    return results_root / envelope["proof_id"] / f"r{envelope['repetition']}" / f"c{envelope['cut']}" / result_filename(envelope)


def _result_id(payload: Mapping[str, Any]) -> str:
    body = dict(payload)
    body.pop("result_id", None)
    return canonical_hash(body)


def make_envelope(
    manifest: Mapping[str, Any],
    lineages: Mapping[str, Any],
    case: Mapping[str, Any],
    compact_evidence: Mapping[str, Any],
    worker: Mapping[str, Any],
    attestation: Mapping[str, Any],
    raw_artifacts: Mapping[str, Mapping[str, Any]],
    timing: Mapping[str, Any],
    shard_id: str,
) -> dict[str, Any]:
    validate_manifest(manifest)
    repetition, cut = case["repetition"], case["cut"]
    lineage = lineage_for_case(lineages, repetition, case["lineage_id"])
    compact = validate_compact_evidence(compact_evidence, cut)
    validate_worker_attestation(attestation, manifest, case, worker)
    if any(not isinstance(name, str) for name in raw_artifacts):
        raise DistributedProofError("raw artifact names must be strings")
    if not raw_artifacts:
        raise DistributedProofError("result must retain at least one raw artifact hash")
    artifacts: dict[str, dict[str, Any]] = {}
    for name, detail in raw_artifacts.items():
        if not isinstance(detail, Mapping):
            raise DistributedProofError(f"raw artifact {name} metadata must be an object")
        size = detail.get("size")
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise DistributedProofError(f"raw artifact {name} size must be non-negative")
        artifacts[name] = {
            "sha256": _hash(detail.get("sha256"), f"raw artifact {name}"),
            "size": size,
        }
    missing = sorted((set(REQUIRED_RAW_ARTIFACTS) | REQUIRED_LOCAL_EVIDENCE)
                     - set(artifacts))
    if missing:
        raise DistributedProofError(
            "result lacks required locally verified artifacts: " + ", ".join(missing))
    run = compact["final_state_verification"]
    commit = compact["committed_operation_verification"]
    for name, field in REQUIRED_RAW_ARTIFACTS.items():
        expected = commit[field] if name == "fault-committed-flash.bin" else run[field]
        if artifacts[name]["sha256"] != expected:
            raise DistributedProofError(
                f"raw artifact {name} does not match local verifier evidence")
    if not isinstance(timing, Mapping) or any(
        isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0
        for value in timing.values()
    ):
        raise DistributedProofError("timing values must be non-negative numbers")
    payload: dict[str, Any] = {
        "schema": RESULT_SCHEMA,
        "version": SCHEMA_VERSION,
        "proof_id": manifest["proof_id"],
        "shard_id": shard_id,
        "repetition": repetition,
        "cut": cut,
        "root_lineage_id": lineage["root_lineage_id"],
        "lineage_id": case["lineage_id"],
        "checkpoint_id": case["checkpoint_id"],
        "checkpoint_path": case["checkpoint_path"],
        "checkpoint_sha256": case["checkpoint_sha256"],
        "execution_mode_hash": case["execution_mode_hash"],
        "worker": {
            "worker_id": worker["worker_id"],
            "architecture": worker["architecture"],
            "build_identity": worker["build_identity"],
            "executor_id": worker["executor_id"],
            "runtime_image_id": worker["runtime_image_id"],
        },
        "attestations": {
            "environment_hash": attestation["environment_hash"],
            "offline_hash": attestation["offline_hash"],
            "unprivileged_hash": attestation["unprivileged_hash"],
        },
        "matrix_row": compact["matrix_row"],
        "verifier": {
            "run": compact["final_state_verification"],
            "commit": compact["committed_operation_verification"],
        },
        "compact_evidence": compact,
        "raw_artifacts": artifacts,
        "timing": dict(timing),
    }
    payload["result_id"] = _result_id(payload)
    return payload


def validate_envelope(
    envelope: Mapping[str, Any],
    manifest: Mapping[str, Any],
    lineages: Mapping[str, Any],
    filename: str | None = None,
    expected_shard_id: str | None = None,
    expected_worker: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    validate_manifest(manifest)
    if envelope.get("schema") != RESULT_SCHEMA or envelope.get("version") != SCHEMA_VERSION:
        raise DistributedProofError("unsupported result schema or version")
    if envelope.get("proof_id") != manifest["proof_id"]:
        raise DistributedProofError("result proof_id does not match manifest")
    repetition, cut = envelope.get("repetition"), envelope.get("cut")
    if (not isinstance(repetition, int)
            or repetition not in lineages.get("by_repetition", {})):
        raise DistributedProofError("result repetition is not covered by lineage manifests")
    lineage = lineage_for_case(lineages, repetition, envelope.get("lineage_id"))
    if not isinstance(cut, int) or not 1 <= cut <= len(lineage["cuts"]):
        raise DistributedProofError("result cut is outside its checkpoint materialization")
    if envelope.get("root_lineage_id") != lineage["root_lineage_id"]:
        raise DistributedProofError("result root_lineage_id mismatch")
    for field in ("checkpoint_id", "checkpoint_path", "checkpoint_sha256"):
        if envelope.get(field) != lineage["cuts"][cut - 1][field]:
            raise DistributedProofError(f"result {field} mismatch")
    if envelope.get("execution_mode_hash") != lineage["execution_mode_hash"]:
        raise DistributedProofError("result execution mode mismatch")
    validate_execution_mode(manifest, envelope["execution_mode_hash"])
    if envelope.get("shard_id") is not None:
        _hash(envelope.get("shard_id"), "shard_id")
    if expected_shard_id is not None and envelope.get("shard_id") != expected_shard_id:
        raise DistributedProofError("result shard_id mismatch")
    worker = _object(envelope.get("worker"), "result worker")
    if (worker.get("architecture") != lineage["architecture"]
            or worker.get("build_identity") != lineage["build_identity"]
            or worker.get("executor_id") != lineage["executor_id"]):
        raise DistributedProofError("result worker identity does not match lineage")
    if expected_worker is not None:
        for field in ("worker_id", "architecture", "build_identity", "executor_id",
                      "runtime_image_id"):
            if worker.get(field) != expected_worker.get(field):
                raise DistributedProofError(f"result worker {field} does not match shard")
    attestations = _object(envelope.get("attestations"), "result attestations")
    for field in ("environment_hash", "offline_hash", "unprivileged_hash"):
        _hash(attestations.get(field), f"result attestations.{field}")
        if (expected_worker is not None
                and attestations.get(field)
                != expected_worker.get("attestation_hashes", {}).get(field)):
            raise DistributedProofError(f"result attestation {field} does not match shard")
    compact = validate_compact_evidence(_object(envelope.get("compact_evidence"), "compact_evidence"), cut)
    branch_operation_count = compact["final_state_verification"].get("operation_count")
    if (not isinstance(branch_operation_count, int)
            or isinstance(branch_operation_count, bool)
            or branch_operation_count < cut):
        raise DistributedProofError(
            "local verifier trace does not contain the selected fault operation")
    row = validate_matrix_row(_object(envelope.get("matrix_row"), "matrix_row"), cut)
    cut_spec = lineage["cuts"][cut - 1]
    if row["type"] != str(cut_spec["type"]) or int(row["length"]) != int(cut_spec["length"]):
        raise DistributedProofError("matrix row operation shape does not match lineage")
    expected_address = (int(cut_spec["address"], 16)
                        if isinstance(cut_spec["address"], str) else int(cut_spec["address"]))
    if int(row["address"], 16) != expected_address:
        raise DistributedProofError("matrix row address does not match lineage")
    if row != compact["matrix_row"]:
        raise DistributedProofError("result matrix row differs from compact evidence")
    verifier = _object(envelope.get("verifier"), "result verifier")
    if verifier.get("run") != compact["final_state_verification"] or verifier.get("commit") != compact["committed_operation_verification"]:
        raise DistributedProofError("result verifier JSON differs from compact evidence")
    raw = _object(envelope.get("raw_artifacts"), "result raw_artifacts")
    for name, detail in raw.items():
        if not isinstance(name, str):
            raise DistributedProofError("raw artifact names must be strings")
        detail = _object(detail, f"raw artifact {name}")
        _hash(detail.get("sha256"), f"raw artifact {name}.sha256")
        size = detail.get("size")
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise DistributedProofError(f"raw artifact {name}.size is invalid")
    missing = sorted((set(REQUIRED_RAW_ARTIFACTS) | REQUIRED_LOCAL_EVIDENCE)
                     - set(raw))
    if missing:
        raise DistributedProofError(
            "result lacks required artifact metadata: " + ", ".join(missing))
    for name, field in REQUIRED_RAW_ARTIFACTS.items():
        expected = (compact["committed_operation_verification"][field]
                    if name == "fault-committed-flash.bin"
                    else compact["final_state_verification"][field])
        if raw[name]["sha256"] != expected:
            raise DistributedProofError(
                f"result raw artifact {name} is not bound to verifier evidence")
    timing = _object(envelope.get("timing"), "result timing")
    if any(isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0
           for value in timing.values()):
        raise DistributedProofError("result timing is invalid")
    result_id = _hash(envelope.get("result_id"), "result_id")
    if result_id != _result_id(envelope):
        raise DistributedProofError("result_id does not match canonical result payload")
    if filename is not None and filename != result_filename(envelope):
        raise DistributedProofError("result filename does not match content identity")
    return dict(envelope)


def hash_directory_artifacts(directory: Path) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for path in sorted(directory.iterdir()):
        if path.is_file() and path.name not in {"envelope.json"}:
            result[path.name] = {"sha256": sha256_file(path), "size": path.stat().st_size}
    if not result:
        raise DistributedProofError("case produced no raw artifacts")
    return result


def atomic_publish_result(path: Path, envelope: Mapping[str, Any]) -> None:
    """Publish using an O_EXCL temp file plus non-replacing hard-link rename.

    A hard link gives the final name atomically without the replacement behavior
    of os.rename when another process has already published a result.
    """
    if path.name != result_filename(envelope):
        raise ImmutableOutputError("result path filename does not match result_id")
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.parent / f".{path.name}.tmp-{os.getpid()}-{secrets.token_hex(6)}"
    try:
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        with os.fdopen(fd, "wb") as stream:
            stream.write(canonical_bytes(envelope) + b"\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temp, path)
        fsync_directory(path.parent)
    except FileExistsError as exc:
        raise ImmutableOutputError(f"refusing to overwrite immutable result: {path}") from exc
    except OSError as exc:
        raise ImmutableOutputError(f"atomic result publication failed for {path}: {exc}") from exc
    finally:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass


def publish_result(path: Path, envelope: Mapping[str, Any]) -> None:
    atomic_publish_result(path, envelope)
