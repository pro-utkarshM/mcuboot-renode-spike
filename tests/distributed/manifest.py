#!/usr/bin/env python3
"""Schemas and canonical identity helpers for the distributed proof prototype.

The format is deliberately boring: JSON objects are sorted and compact before
hashing, identities are hashes of payloads with their identity field removed,
and every writer refuses to replace an existing file.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
from pathlib import Path
from typing import Any, Mapping, Sequence

SCHEMA_VERSION = 1
MANIFEST_SCHEMA = "ota.distributed-proof.manifest"
LINEAGE_SCHEMA = "ota.distributed-proof.lineage"
WORKER_PROFILE_SCHEMA = "ota.distributed-proof.worker-profile"
SHARD_SCHEMA = "ota.distributed-proof.shard"
JOURNAL_SCHEMA = "ota.distributed-proof.journal"
ATTESTATION_SCHEMA = "ota.distributed-proof.worker-attestation"
RESULT_SCHEMA = "ota.distributed-proof.result"
SUMMARY_SCHEMA = "ota.distributed-proof.matrix-summary"
HEX64 = re.compile(r"^[0-9a-f]{64}$")
REQUIRED_GATES = {
    "baseline", "matrix", "determinism", "negative-tests",
    "offline-unprivileged", "verifier-self-test",
}


class DistributedProofError(ValueError):
    """A distributed proof input or output is not safe to consume."""


class ImmutableOutputError(DistributedProofError):
    """An immutable output already exists or could not be published safely."""


def canonical_bytes(value: Any) -> bytes:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise DistributedProofError(f"value is not canonical JSON: {exc}") from exc
    return encoded.encode("utf-8")


def canonical_json(value: Any) -> str:
    return canonical_bytes(value).decode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise DistributedProofError(f"cannot hash {path}: {exc}") from exc
    return digest.hexdigest()


def canonical_hash(value: Any) -> str:
    return sha256_bytes(canonical_bytes(value))


def _identity(payload: Mapping[str, Any], field: str) -> str:
    body = dict(payload)
    body.pop(field, None)
    return canonical_hash(body)


def derive_proof_id(payload: Mapping[str, Any]) -> str:
    return _identity(payload, "proof_id")


def derive_lineage_id(payload: Mapping[str, Any]) -> str:
    return _identity(payload, "lineage_id")


def derive_root_lineage_id(
        proof_id: str, repetition: int, lineage_seed: str,
        clean_trace_sha256: str, baseline_flash_sha256: str,
        operation_definition_hash: str,
        root_clean_execution_sha256: str) -> str:
    return canonical_hash({
        "proof_id": proof_id,
        "repetition": repetition,
        "lineage_seed": lineage_seed,
        "clean_trace_sha256": clean_trace_sha256,
        "baseline_flash_sha256": baseline_flash_sha256,
        "operation_definition_hash": operation_definition_hash,
        "root_clean_execution_sha256": root_clean_execution_sha256,
    })


def derive_checkpoint_id(
    root_lineage_id: str,
    repetition: int,
    cut: int,
    architecture: str,
    build_identity: str,
    execution_mode_hash: str,
    checkpoint_path: str,
    checkpoint_sha256: str,
) -> str:
    return canonical_hash({
        "root_lineage_id": root_lineage_id,
        "repetition": repetition,
        "cut": cut,
        "architecture": architecture,
        "build_identity": build_identity,
        "execution_mode_hash": execution_mode_hash,
        "checkpoint_path": checkpoint_path,
        "checkpoint_sha256": checkpoint_sha256,
    })


def _json_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise DistributedProofError(f"cannot read {label} JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise DistributedProofError(f"{label} must be a JSON object: {path}")
    return value


def _require_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise DistributedProofError(f"{field} must be a non-empty string")
    return value


def _require_hash(value: Any, field: str) -> str:
    value = _require_string(value, field)
    if not HEX64.fullmatch(value):
        raise DistributedProofError(f"{field} must be a lowercase SHA-256 hex digest")
    return value


def _input_entries(manifest: Mapping[str, Any]) -> list[dict[str, str]]:
    raw = manifest.get("inputs")
    if isinstance(raw, Mapping):
        raw = [{"path": path, "sha256": digest} for path, digest in raw.items()]
    if not isinstance(raw, list) or not raw:
        raise DistributedProofError("manifest inputs must be a non-empty list")
    entries: list[dict[str, str]] = []
    seen: set[str] = set()
    for index, item in enumerate(raw):
        if not isinstance(item, Mapping):
            raise DistributedProofError(f"manifest inputs[{index}] must be an object")
        path = _require_string(item.get("path"), f"inputs[{index}].path")
        digest = _require_hash(item.get("sha256"), f"inputs[{index}].sha256")
        if path in seen:
            raise DistributedProofError(f"duplicate declared input path: {path}")
        seen.add(path)
        entries.append({"path": path, "sha256": digest})
    return entries


def _expected_cut_count(manifest: Mapping[str, Any]) -> int:
    matrix = manifest.get("matrix", {})
    value = manifest.get("cut_count", manifest.get("operation_count"))
    if value is None and isinstance(matrix, Mapping):
        value = matrix.get("expected_cut_points", matrix.get("cut_count"))
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise DistributedProofError("manifest must declare a positive cut_count")
    return value


def validate_manifest(manifest: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(manifest, Mapping):
        raise DistributedProofError("proof manifest must be a JSON object")
    if manifest.get("schema") != MANIFEST_SCHEMA:
        raise DistributedProofError("unsupported proof manifest schema")
    if manifest.get("version") != SCHEMA_VERSION:
        raise DistributedProofError("unsupported proof manifest version")
    repetitions = manifest.get("repetitions")
    if repetitions != 5 or isinstance(repetitions, bool):
        raise DistributedProofError("the proof manifest must require exactly five repetitions")
    _input_entries(manifest)
    checkpoint_root = _require_string(
        manifest.get("checkpoint_root"), "checkpoint_root")
    if Path(checkpoint_root).is_absolute():
        raise DistributedProofError("checkpoint_root must be relative to the manifest")
    cut_count = _expected_cut_count(manifest)
    _require_hash(manifest.get("proof_contract_sha256"), "proof_contract_sha256")
    gates = manifest.get("required_gates")
    if not isinstance(gates, list) or set(gates) != REQUIRED_GATES or len(gates) != len(REQUIRED_GATES):
        raise DistributedProofError("manifest required_gates do not match the proof contract")
    trace = manifest.get("canonical_trace")
    if not isinstance(trace, Mapping):
        raise DistributedProofError("manifest canonical_trace must be an object")
    _require_hash(trace.get("sha256"), "canonical_trace.sha256")
    _require_hash(trace.get("operations_sha256"), "canonical_trace.operations_sha256")
    if trace.get("operation_count") != cut_count:
        raise DistributedProofError("canonical trace operation count does not match cut_count")
    fixtures = manifest.get("negative_fixtures")
    if not isinstance(fixtures, Mapping) or set(fixtures) != {
            "premature-confirm", "erase-after-confirm"}:
        raise DistributedProofError("manifest must bind both negative fixtures")
    for name, digest in fixtures.items():
        _require_hash(digest, f"negative_fixtures[{name}]")
    builds = manifest.get("renode_builds")
    if not isinstance(builds, Mapping) or not builds:
        raise DistributedProofError("manifest must bind at least one Renode build")
    for architecture, build in builds.items():
        if not isinstance(architecture, str) or not architecture or not isinstance(build, Mapping):
            raise DistributedProofError("invalid Renode build declaration")
        _require_string(build.get("build_identity"), f"renode_builds[{architecture}].build_identity")
        _require_hash(build.get("sha256"), f"renode_builds[{architecture}].sha256")
    executors = manifest.get("executors")
    if not isinstance(executors, Mapping) or not executors:
        raise DistributedProofError("manifest must bind architecture-specific executors")
    for executor_id, executor in executors.items():
        _require_string(executor_id, "executor id")
        if not isinstance(executor, Mapping):
            raise DistributedProofError("executor profile must be an object")
        architecture = _require_string(
            executor.get("architecture"), f"executor {executor_id} architecture")
        build_identity = _require_string(
            executor.get("build_identity"), f"executor {executor_id} build_identity")
        if (architecture not in builds
                or builds[architecture].get("build_identity") != build_identity):
            raise DistributedProofError("executor uses an unregistered Renode build")
        _require_hash(executor.get("execution_mode_hash"),
                      f"executor {executor_id} execution_mode_hash")
        argv = executor.get("argv")
        if (not isinstance(argv, list) or not argv
                or any(not isinstance(value, str) or not value for value in argv)):
            raise DistributedProofError("executor argv must be a non-empty string list")
        if executor.get("sha256") != canonical_hash(argv):
            raise DistributedProofError("executor.sha256 does not match canonical argv")
        _require_hash(executor.get("benchmark_argv_sha256"),
                      f"executor {executor_id} benchmark_argv_sha256")
        if argv != ["test-only-callable"]:
            isolation = executor.get("isolation")
            if isolation != {
                    "network": "none",
                    "cap_drop": "ALL",
                    "no_new_privileges": True,
                    "runtime_user": "non-root"}:
                raise DistributedProofError(
                    "production executor must bind the offline/unprivileged isolation contract")
    seeds = manifest.get("lineage_seeds")
    if not isinstance(seeds, list) or len(seeds) != 5:
        raise DistributedProofError("manifest must bind five lineage seeds")
    checked_seeds = [_require_hash(seed, "lineage seed") for seed in seeds]
    if len(set(checked_seeds)) != 5:
        raise DistributedProofError("lineage seeds must be unique")
    workers = manifest.get("qualified_workers")
    if not isinstance(workers, Mapping) or not workers:
        raise DistributedProofError("manifest must register qualified workers")
    for worker_id, worker in workers.items():
        _require_string(worker_id, "qualified worker id")
        if not isinstance(worker, Mapping):
            raise DistributedProofError("qualified worker must be an object")
        architecture = _require_string(worker.get("architecture"), "qualified worker architecture")
        build_identity = _require_string(worker.get("build_identity"), "qualified worker build_identity")
        if (architecture not in builds
                or builds[architecture].get("build_identity") != build_identity):
            raise DistributedProofError("qualified worker uses an unregistered Renode build")
        executor_id = _require_string(
            worker.get("executor_id"), "qualified worker executor_id")
        _require_string(worker.get("runtime_image_id"),
                        "qualified worker runtime_image_id")
        executor = executors.get(executor_id)
        if (not isinstance(executor, Mapping)
                or executor.get("architecture") != architecture
                or executor.get("build_identity") != build_identity):
            raise DistributedProofError("qualified worker uses an incompatible executor")
        attestations = worker.get("attestation_hashes")
        if not isinstance(attestations, Mapping):
            raise DistributedProofError("qualified worker lacks attestation hashes")
        for field in ("environment_hash", "offline_hash", "unprivileged_hash"):
            _require_hash(attestations.get(field), f"qualified worker {worker_id} {field}")
    proof_id = _require_hash(manifest.get("proof_id"), "proof_id")
    if proof_id != derive_proof_id(manifest):
        raise DistributedProofError("proof_id does not match canonical manifest payload")
    environment = manifest.get("environment", {})
    if not isinstance(environment, Mapping):
        raise DistributedProofError("manifest environment must be an object")
    for name, digest in environment.get("hashes", {}).items():
        _require_hash(digest, f"environment.hashes[{name}]")
    modes = manifest.get("execution_modes", {})
    if not isinstance(modes, (Mapping, list)):
        raise DistributedProofError("execution_modes must be an object or list")
    qualified = qualified_execution_modes(manifest)
    if not qualified:
        raise DistributedProofError("manifest must qualify at least one execution mode")
    return dict(manifest)


def create_manifest(payload: Mapping[str, Any]) -> dict[str, Any]:
    body = dict(payload)
    body.setdefault("schema", MANIFEST_SCHEMA)
    body.setdefault("version", SCHEMA_VERSION)
    body["proof_id"] = derive_proof_id(body)
    return validate_manifest(body)


def load_manifest(path: Path) -> dict[str, Any]:
    return validate_manifest(_json_object(path, "proof manifest"))


def _resolve(base_dir: Path, value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else base_dir / path


def hash_declared_inputs(manifest: Mapping[str, Any], base_dir: Path) -> dict[str, str]:
    validate_manifest(manifest)
    actual: dict[str, str] = {}
    for entry in _input_entries(manifest):
        path = _resolve(base_dir, entry["path"])
        digest = sha256_file(path)
        if digest != entry["sha256"]:
            raise DistributedProofError(
                f"declared input hash mismatch for {entry['path']}: {digest} != {entry['sha256']}"
            )
        actual[entry["path"]] = digest
    return actual


def local_environment_hashes(manifest: Mapping[str, Any], base_dir: Path) -> dict[str, str]:
    environment = manifest.get("environment", {})
    result: dict[str, str] = {}
    files = environment.get("files", []) if isinstance(environment, Mapping) else []
    if not isinstance(files, list):
        raise DistributedProofError("environment.files must be a list")
    for index, item in enumerate(files):
        if not isinstance(item, Mapping):
            raise DistributedProofError(f"environment.files[{index}] must be an object")
        path = _require_string(item.get("path"), f"environment.files[{index}].path")
        expected = _require_hash(item.get("sha256"), f"environment.files[{index}].sha256")
        actual = sha256_file(_resolve(base_dir, path))
        if actual != expected:
            raise DistributedProofError(f"environment hash mismatch for {path}")
        result[path] = actual
    return result


def validate_local_environment(
    manifest: Mapping[str, Any],
    base_dir: Path,
    local_hashes: Mapping[str, str] | None = None,
) -> dict[str, str]:
    expected = manifest.get("environment", {}).get("hashes", {})
    if not isinstance(expected, Mapping):
        raise DistributedProofError("environment.hashes must be an object")
    result = local_environment_hashes(manifest, base_dir)
    for name, digest in expected.items():
        expected_digest = _require_hash(digest, f"environment.hashes[{name}]")
        actual = local_hashes.get(name) if local_hashes is not None else result.get(name)
        if actual is None:
            raise DistributedProofError(
                f"no local environment hash supplied for {name}; refusing an unverified worker"
            )
        if actual != expected_digest:
            raise DistributedProofError(f"local environment hash mismatch for {name}")
        result[name] = actual
    return result


def qualified_execution_modes(manifest: Mapping[str, Any]) -> set[str]:
    modes = manifest.get("execution_modes", {})
    qualified: set[str] = set()
    if isinstance(modes, Mapping):
        for mode_hash, detail in modes.items():
            if isinstance(detail, Mapping) and detail.get("qualified") is True:
                qualified.add(_require_hash(mode_hash, "execution mode hash"))
            elif detail is True:
                qualified.add(_require_hash(mode_hash, "execution mode hash"))
    else:
        for item in modes:
            if isinstance(item, Mapping) and item.get("qualified") is True:
                qualified.add(_require_hash(item.get("hash"), "execution mode hash"))
    for value in manifest.get("qualified_execution_mode_hashes", []):
        qualified.add(_require_hash(value, "qualified execution mode hash"))
    return qualified


def validate_execution_mode(manifest: Mapping[str, Any], mode_hash: str) -> None:
    if mode_hash not in qualified_execution_modes(manifest):
        raise DistributedProofError(f"execution mode is not qualified: {mode_hash}")


def validate_lineage(lineage: Mapping[str, Any], manifest: Mapping[str, Any]) -> dict[str, Any]:
    if lineage.get("schema") != LINEAGE_SCHEMA or lineage.get("version") != SCHEMA_VERSION:
        raise DistributedProofError("unsupported lineage schema or version")
    if lineage.get("proof_id") != manifest["proof_id"]:
        raise DistributedProofError("lineage proof_id does not match manifest")
    repetition = lineage.get("repetition")
    if not isinstance(repetition, int) or not 1 <= repetition <= manifest["repetitions"]:
        raise DistributedProofError("lineage repetition is outside manifest 1..5 bound")
    architecture = _require_string(lineage.get("architecture"), "lineage architecture")
    build_identity = _require_string(lineage.get("build_identity"), "lineage build_identity")
    build = manifest["renode_builds"].get(architecture)
    if not isinstance(build, Mapping) or build.get("build_identity") != build_identity:
        raise DistributedProofError("lineage uses an unregistered architecture/build")
    executor_id = _require_string(lineage.get("executor_id"), "lineage executor_id")
    executor = manifest["executors"].get(executor_id)
    if (not isinstance(executor, Mapping)
            or executor.get("architecture") != architecture
            or executor.get("build_identity") != build_identity):
        raise DistributedProofError("lineage uses an incompatible executor")
    if lineage.get("lineage_seed") != manifest["lineage_seeds"][repetition - 1]:
        raise DistributedProofError("lineage seed does not match its repetition")
    if lineage.get("clean_trace_sha256") != manifest["canonical_trace"]["sha256"]:
        raise DistributedProofError("lineage clean trace does not match the manifest")
    for field in ("baseline_flash_sha256", "root_clean_execution_sha256",
                  "clean_execution_evidence_sha256",
                  "checkpoint_manifest_sha256"):
        _require_hash(lineage.get(field), f"lineage {field}")
    root_lineage_id = _require_hash(
        lineage.get("root_lineage_id"), "root_lineage_id")
    _require_hash(lineage.get("execution_mode_hash"), "lineage execution_mode_hash")
    validate_execution_mode(manifest, lineage["execution_mode_hash"])
    if executor.get("execution_mode_hash") != lineage["execution_mode_hash"]:
        raise DistributedProofError("lineage execution mode does not match its executor")
    lineage_id = _require_hash(lineage.get("lineage_id"), "lineage_id")
    if lineage_id != derive_lineage_id(lineage):
        raise DistributedProofError("lineage_id does not match canonical lineage payload")
    cuts = lineage.get("cuts")
    expected = _expected_cut_count(manifest)
    if not isinstance(cuts, list) or len(cuts) != expected:
        raise DistributedProofError(f"lineage must contain exactly {expected} cuts")
    for index, cut in enumerate(cuts, 1):
        if not isinstance(cut, Mapping):
            raise DistributedProofError(f"lineage cuts[{index - 1}] must be an object")
        number = cut.get("cut", cut.get("cut_point"))
        if number != index:
            raise DistributedProofError("lineage cuts must be contiguous and sorted")
        if cut.get("operation", index) != index:
            raise DistributedProofError("lineage operation numbers must be contiguous")
        if cut.get("type") not in {"program", "erase"}:
            raise DistributedProofError("lineage cut type must be program or erase")
        if not isinstance(cut.get("length"), int) or cut["length"] < 1:
            raise DistributedProofError("lineage cut length must be positive")
        checkpoint_path = _require_string(
            cut.get("checkpoint_path"), "lineage cut checkpoint_path")
        relative_checkpoint = Path(checkpoint_path)
        if relative_checkpoint.is_absolute() or ".." in relative_checkpoint.parts:
            raise DistributedProofError(
                "lineage checkpoint paths must be safe relative paths")
        checkpoint_sha256 = _require_hash(
            cut.get("checkpoint_sha256"), "lineage cut checkpoint_sha256")
        checkpoint_id = _require_hash(
            cut.get("checkpoint_id"), "lineage cut checkpoint_id")
        if checkpoint_id != derive_checkpoint_id(
                root_lineage_id, repetition, index, architecture,
                build_identity, lineage["execution_mode_hash"],
                checkpoint_path, checkpoint_sha256):
            raise DistributedProofError(
                "lineage checkpoint_id does not bind its artifact and execution state")
        address = cut.get("address")
        try:
            address_number = int(address, 16) if isinstance(address, str) else int(address)
        except (TypeError, ValueError) as exc:
            raise DistributedProofError("lineage cut address must be numeric") from exc
        if address_number < 0:
            raise DistributedProofError("lineage cut address must not be negative")
        costs = cut.get("phase_costs")
        if not isinstance(costs, Mapping) or not costs:
            raise DistributedProofError("every lineage cut needs non-empty phase_costs")
        if any(isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0
               for value in costs.values()):
            raise DistributedProofError("lineage phase costs must be non-negative numbers")
    operation_hash = canonical_hash([
        {
            "operation": index,
            "type": cut["type"],
            "address": f"0x{(int(cut['address'], 16) if isinstance(cut['address'], str) else int(cut['address'])):08x}",
            "length": cut["length"],
        }
        for index, cut in enumerate(cuts, 1)
    ])
    if operation_hash != manifest["canonical_trace"]["operations_sha256"]:
        raise DistributedProofError("lineage operation definition does not match the canonical trace")
    if lineage.get("operation_definition_hash") != operation_hash:
        raise DistributedProofError("lineage operation_definition_hash is invalid")
    expected_root = derive_root_lineage_id(
        manifest["proof_id"], repetition, lineage["lineage_seed"],
        lineage["clean_trace_sha256"], lineage["baseline_flash_sha256"],
        operation_hash, lineage["root_clean_execution_sha256"])
    if root_lineage_id != expected_root:
        raise DistributedProofError(
            "root_lineage_id does not bind the repetition's verified clean root")
    return dict(lineage)


def load_lineage(path: Path, manifest: Mapping[str, Any]) -> dict[str, Any]:
    return validate_lineage(_json_object(path, "lineage"), manifest)


def validate_lineages(
        lineages: Sequence[Mapping[str, Any]],
        manifest: Mapping[str, Any]) -> dict[str, Any]:
    if len(lineages) < manifest["repetitions"]:
        raise DistributedProofError(
            "at least one checkpoint materialization is required for every repetition")
    by_id: dict[str, dict[str, Any]] = {}
    by_rep: dict[int, list[dict[str, Any]]] = {}
    roots: dict[int, str] = {}
    root_inputs: dict[int, tuple[str, str, str, str]] = {}
    materializations: set[tuple[int, str, str, str]] = set()
    for lineage in lineages:
        checked = validate_lineage(lineage, manifest)
        repetition = checked["repetition"]
        lineage_id = checked["lineage_id"]
        if lineage_id in by_id:
            raise DistributedProofError("lineage_id values must be unique across repetitions")
        root = roots.setdefault(repetition, checked["root_lineage_id"])
        if root != checked["root_lineage_id"]:
            raise DistributedProofError(
                f"checkpoint materializations for repetition {repetition} have different roots")
        clean_root = (
            checked["clean_trace_sha256"], checked["baseline_flash_sha256"],
            checked["operation_definition_hash"],
            checked["root_clean_execution_sha256"],
        )
        existing_clean_root = root_inputs.setdefault(repetition, clean_root)
        if existing_clean_root != clean_root:
            raise DistributedProofError(
                f"checkpoint materializations for repetition {repetition} do not share a clean root")
        key = (repetition, checked["architecture"], checked["build_identity"],
               checked["execution_mode_hash"], checked["executor_id"])
        if key in materializations:
            raise DistributedProofError(
                "duplicate architecture/build/execution-mode materialization")
        materializations.add(key)
        by_id[lineage_id] = checked
        by_rep.setdefault(repetition, []).append(checked)
    expected_repetitions = set(range(1, manifest["repetitions"] + 1))
    if set(by_rep) != expected_repetitions:
        raise DistributedProofError("lineage repetitions must cover every manifest repetition")
    if len(set(roots.values())) != manifest["repetitions"]:
        raise DistributedProofError("root lineage IDs must be unique across repetitions")
    for values in by_rep.values():
        values.sort(key=lambda item: item["lineage_id"])
    return {"by_id": by_id, "by_repetition": by_rep, "roots": roots}


def lineage_for_case(
        lineages: Mapping[str, Any], repetition: int,
        lineage_id: str) -> dict[str, Any]:
    lineage = lineages.get("by_id", {}).get(lineage_id)
    if not isinstance(lineage, Mapping) or lineage.get("repetition") != repetition:
        raise DistributedProofError(
            "case references an unknown checkpoint materialization")
    return dict(lineage)


def verify_case_checkpoint(
    manifest: Mapping[str, Any],
    manifest_path: Path,
    lineages: Mapping[str, Any],
    case: Mapping[str, Any],
) -> Path:
    lineage = lineage_for_case(
        lineages, case["repetition"], case["lineage_id"])
    cut = lineage["cuts"][case["cut"] - 1]
    for field in ("checkpoint_id", "checkpoint_path", "checkpoint_sha256"):
        if case.get(field) != cut[field]:
            raise DistributedProofError(
                f"case {field} does not match its checkpoint materialization")
    root = (manifest_path.parent / manifest["checkpoint_root"]).resolve()
    path = (root / cut["checkpoint_path"]).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise DistributedProofError("checkpoint path escapes checkpoint_root") from exc
    actual = sha256_file(path)
    if actual != cut["checkpoint_sha256"]:
        raise DistributedProofError(
            f"checkpoint artifact hash mismatch for r{case['repetition']}-c{case['cut']}")
    return path


def validate_worker_profile(
        profile: Mapping[str, Any],
        manifest: Mapping[str, Any] | None = None) -> dict[str, Any]:
    if profile.get("schema") != WORKER_PROFILE_SCHEMA or profile.get("version") != SCHEMA_VERSION:
        raise DistributedProofError("unsupported worker profile schema or version")
    for field in ("worker_id", "architecture", "build_identity", "executor_id",
                  "runtime_image_id"):
        _require_string(profile.get(field), f"worker profile {field}")
    for field in ("cases_per_sec", "cost_rate"):
        value = profile.get(field)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
            raise DistributedProofError(f"worker profile {field} must be positive")
    phase_rates = profile.get("phase_rates", {})
    if not isinstance(phase_rates, Mapping):
        raise DistributedProofError("worker profile phase_rates must be an object")
    for phase, value in phase_rates.items():
        if (not isinstance(phase, str) or not phase or isinstance(value, bool)
                or not isinstance(value, (int, float)) or value <= 0):
            raise DistributedProofError("worker profile phase rates must be positive numbers")
    attestations = profile.get("attestation_hashes")
    if not isinstance(attestations, Mapping):
        raise DistributedProofError("worker profile attestation_hashes must be an object")
    for field in ("environment_hash", "offline_hash", "unprivileged_hash"):
        _require_hash(attestations.get(field), f"worker profile attestation_hashes.{field}")
    if manifest is not None:
        registered = manifest["qualified_workers"].get(profile["worker_id"])
        if not isinstance(registered, Mapping):
            raise DistributedProofError("worker profile is not registered by the proof manifest")
        for field in ("architecture", "build_identity", "executor_id",
                      "runtime_image_id", "attestation_hashes"):
            if profile.get(field) != registered.get(field):
                raise DistributedProofError(
                    f"worker profile {field} does not match the proof manifest")
    return dict(profile)


def load_worker_profile(path: Path) -> dict[str, Any]:
    return validate_worker_profile(_json_object(path, "worker profile"))


def write_immutable_json(path: Path, payload: Any) -> None:
    """Publish canonical JSON without ever replacing an existing path."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.parent / (
        f".{path.name}.tmp-{os.getpid()}-{secrets.token_hex(6)}")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    try:
        fd = os.open(temp, flags, 0o644)
        with os.fdopen(fd, "wb") as stream:
            stream.write(canonical_bytes(payload) + b"\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temp, path)
        fsync_directory(path.parent)
    except FileExistsError as exc:
        raise ImmutableOutputError(f"immutable output already exists: {path}") from exc
    except OSError as exc:
        raise ImmutableOutputError(f"cannot publish immutable output {path}: {exc}") from exc
    finally:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass


def fsync_directory(path: Path) -> None:
    """Persist a same-filesystem publication directory entry."""
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
