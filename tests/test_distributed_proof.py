#!/usr/bin/env python3
"""Unit tests for the opt-in bounded distributed proof prototype."""

from __future__ import annotations

import csv
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests.distributed.aggregate import aggregate_results
from tests.distributed.benchmark_worker import benchmark
from tests.distributed.deploy import (
    init_layout,
    install_results,
    install_shards,
    stage_worker_shards,
    status as deployment_status,
    validate_aggregation_summary,
    verify_worker_checkpoints,
)
from tests.distributed.evidence import (
    MATRIX_COLUMNS,
    atomic_publish_result,
    make_envelope,
    result_path,
    validate_compact_evidence,
)
from tests.distributed.generate_shards import generate_shards
from tests.distributed.generate_qualification import generate_qualification_shards
from tests.distributed.manifest import (
    ATTESTATION_SCHEMA,
    LINEAGE_SCHEMA,
    SCHEMA_VERSION,
    WORKER_PROFILE_SCHEMA,
    DistributedProofError,
    canonical_hash,
    create_manifest,
    derive_checkpoint_id,
    derive_lineage_id,
    derive_root_lineage_id,
    hash_declared_inputs,
    sha256_bytes,
    validate_lineages,
    write_immutable_json,
)
from tests.distributed.run_shard import run_shard
from tests.verify_state import semantic_uart_text


H = "a" * 64


class DistributedProofTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.input_path = self.root / "fixture.bin"
        self.input_path.write_bytes(b"immutable proof input")
        self.checkpoint_root = self.root / "checkpoints"
        self.checkpoint_root.mkdir()
        self.mode_hash = sha256_bytes(b"offline-unprivileged-mode")
        self.cut_specs = [
            {"cut": 1, "operation": 1, "type": "program", "address": "0x00000000",
             "length": 4, "phase_costs": {"boot": 10, "verify": 2}},
            {"cut": 2, "operation": 2, "type": "erase", "address": "0x00000004",
             "length": 4, "phase_costs": {"boot": 1, "verify": 1}},
        ]
        operation_definition_hash = canonical_hash([
            {"operation": cut["operation"], "type": cut["type"],
             "address": cut["address"], "length": cut["length"]}
            for cut in self.cut_specs
        ])
        lineage_seeds = [sha256_bytes(f"lineage-{value}".encode()) for value in range(1, 6)]
        attestations = {"environment_hash": H, "offline_hash": H,
                        "unprivileged_hash": H}
        self.manifest = create_manifest({
            "schema": "ota.distributed-proof.manifest",
            "version": SCHEMA_VERSION,
            "repetitions": 5,
            "cut_count": 2,
            "checkpoint_root": "checkpoints",
            "inputs": [{"path": "fixture.bin", "sha256": sha256_bytes(self.input_path.read_bytes())}],
            "proof_contract_sha256": H,
            "required_gates": ["baseline", "matrix", "determinism", "negative-tests",
                               "offline-unprivileged", "verifier-self-test"],
            "canonical_trace": {"sha256": H, "operation_count": 2,
                                "operations_sha256": operation_definition_hash},
            "negative_fixtures": {"premature-confirm": H, "erase-after-confirm": H},
            "renode_builds": {
                "logical-x86": {"build_identity": "worker-build-v1", "sha256": H},
                "logical-arm64": {"build_identity": "worker-build-arm64-v1", "sha256": H},
            },
            "lineage_seeds": lineage_seeds,
            "qualified_workers": {
                "worker-a": {"architecture": "logical-x86",
                             "build_identity": "worker-build-v1",
                             "executor_id": "executor-x86",
                             "runtime_image_id": "sha256:test-x86",
                             "attestation_hashes": attestations},
                "worker-b": {"architecture": "logical-x86",
                             "build_identity": "worker-build-v1",
                             "executor_id": "executor-x86",
                             "runtime_image_id": "sha256:test-x86",
                             "attestation_hashes": attestations},
                "worker-pi": {"architecture": "logical-arm64",
                              "build_identity": "worker-build-arm64-v1",
                              "executor_id": "executor-arm64",
                              "runtime_image_id": "sha256:test-arm64",
                              "attestation_hashes": attestations},
            },
            "executors": {
                "executor-x86": {
                    "architecture": "logical-x86",
                    "build_identity": "worker-build-v1",
                    "execution_mode_hash": self.mode_hash,
                    "argv": ["test-only-callable"],
                    "sha256": canonical_hash(["test-only-callable"]),
                    "benchmark_argv_sha256": H,
                },
                "executor-arm64": {
                    "architecture": "logical-arm64",
                    "build_identity": "worker-build-arm64-v1",
                    "execution_mode_hash": self.mode_hash,
                    "argv": ["test-only-callable"],
                    "sha256": canonical_hash(["test-only-callable"]),
                    "benchmark_argv_sha256": H,
                },
            },
            "environment": {"files": [], "hashes": {}},
            "execution_modes": {self.mode_hash: {"qualified": True, "offline": True, "unprivileged": True}},
        })
        self.manifest_path = self.root / "manifest.json"
        write_immutable_json(self.manifest_path, self.manifest)
        self.lineages: list[dict[str, object]] = []
        self.lineage_paths: list[Path] = []
        for repetition in range(1, 6):
            root_clean = sha256_bytes(f"root-clean-{repetition}".encode())
            root_lineage_id = derive_root_lineage_id(
                self.manifest["proof_id"], repetition,
                lineage_seeds[repetition - 1], H, H,
                operation_definition_hash, root_clean)
            repetition_cuts = []
            for cut in self.cut_specs:
                checkpoint_path = f"x86/r{repetition}/c{cut['cut']}.save"
                checkpoint_file = self.checkpoint_root / checkpoint_path
                checkpoint_file.parent.mkdir(parents=True, exist_ok=True)
                checkpoint_file.write_bytes(
                    f"x86-checkpoint-{repetition}-{cut['cut']}".encode())
                checkpoint_sha256 = sha256_bytes(checkpoint_file.read_bytes())
                repetition_cuts.append(dict(
                    cut,
                    checkpoint_path=checkpoint_path,
                    checkpoint_sha256=checkpoint_sha256,
                    checkpoint_id=derive_checkpoint_id(
                        root_lineage_id, repetition, cut["cut"],
                        "logical-x86", "worker-build-v1", self.mode_hash,
                        checkpoint_path, checkpoint_sha256),
                ))
            lineage: dict[str, object] = {
                "schema": LINEAGE_SCHEMA,
                "version": SCHEMA_VERSION,
                "proof_id": self.manifest["proof_id"],
                "repetition": repetition,
                "architecture": "logical-x86",
                "build_identity": "worker-build-v1",
                "executor_id": "executor-x86",
                "lineage_seed": lineage_seeds[repetition - 1],
                "root_lineage_id": root_lineage_id,
                "clean_trace_sha256": H,
                "baseline_flash_sha256": H,
                "root_clean_execution_sha256": root_clean,
                "clean_execution_evidence_sha256": sha256_bytes(
                    f"clean-execution-{repetition}".encode()),
                "checkpoint_manifest_sha256": sha256_bytes(
                    f"checkpoint-manifest-{repetition}".encode()),
                "operation_definition_hash": operation_definition_hash,
                "execution_mode_hash": self.mode_hash,
                "cuts": repetition_cuts,
            }
            lineage["lineage_id"] = derive_lineage_id(lineage)
            self.lineages.append(lineage)
            path = self.root / f"lineage-{repetition}.json"
            write_immutable_json(path, lineage)
            self.lineage_paths.append(path)
        self.profiles = [
            {"schema": WORKER_PROFILE_SCHEMA, "version": SCHEMA_VERSION,
             "worker_id": "worker-a", "architecture": "logical-x86",
             "build_identity": "worker-build-v1", "executor_id": "executor-x86",
             "runtime_image_id": "sha256:test-x86",
             "cases_per_sec": 1.0, "cost_rate": 1.0,
             "phase_rates": {"boot": 1.0, "verify": 1.0},
             "attestation_hashes": {"environment_hash": H, "offline_hash": H,
                                    "unprivileged_hash": H}},
            {"schema": WORKER_PROFILE_SCHEMA, "version": SCHEMA_VERSION,
             "worker_id": "worker-b", "architecture": "logical-x86",
             "build_identity": "worker-build-v1", "executor_id": "executor-x86",
             "runtime_image_id": "sha256:test-x86",
             "cases_per_sec": 4.0, "cost_rate": 1.5,
             "phase_rates": {"boot": 4.0, "verify": 2.0},
             "attestation_hashes": {"environment_hash": H, "offline_hash": H,
                                    "unprivileged_hash": H}},
        ]
        self.lineage_map = validate_lineages(self.lineages, self.manifest)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _bulk_artifacts(self, cut: int, repetition: int | None = None) -> dict[str, bytes]:
        operation_type = self.cut_specs[cut - 1]["type"]
        semantic_uart = (
            b"FIRMWARE_VERSION=2.0.0\n"
            b"RAM_BOOT_MARKER_RESET=1\n"
            b"PERSISTENT_SETTING=loaded:generation=1\n"
        )
        diagnostic = (b"" if repetition is None
                      else f"transport-diagnostic-{repetition}".encode())
        return {
            "final-flash.bin": b"verified-final-flash",
            "flash-operations.log": (
                f"op={cut} type={operation_type} address=0x{(cut - 1) * 4:08x} length=4\n"
                f"fault=power-loss after_op={cut}\n").encode(),
            "uart.log": semantic_uart + b"\x00" + diagnostic,
            "mcumgr-image-list.txt": b"image=0 slot=0 version: 2.0.0 flags: active confirmed\n",
            "fault-committed-flash.bin": f"committed-snapshot-{cut}".encode(),
            "fault-operation.txt": f"operation={cut}\n".encode(),
        }

    def _row(self, cut: int, repetition: int | None = None) -> dict[str, str]:
        bulk = self._bulk_artifacts(cut, repetition)
        uart_text = bulk["uart.log"].decode("utf-8", errors="replace")
        return {
            "cut_point": str(cut), "operation": str(cut),
            "type": self.cut_specs[cut - 1]["type"],
            "address": f"0x{(cut - 1) * 4:08x}", "length": "4", "final_image": "v2",
            "boots": "5", "state_valid": "true", "result": "pass",
            "flash_hash": sha256_bytes(bulk["final-flash.bin"]),
            "trace_hash": sha256_bytes(bulk["flash-operations.log"]),
            "uart_semantic_hash": sha256_bytes(
                semantic_uart_text(uart_text).encode()),
            "uart_raw_hash": sha256_bytes(bulk["uart.log"]),
            "mcumgr_hash": sha256_bytes(bulk["mcumgr-image-list.txt"]),
            "fault_snapshot_hash": sha256_bytes(
                bulk["fault-committed-flash.bin"]),
        }

    def _compact(self, cut: int, repetition: int | None = None) -> dict[str, object]:
        row = self._row(cut, repetition)
        run = {
            "result": "pass", "final_image": "v2", "fault_after_operation": cut,
            # Recovery can append operations after the clean selected cut.
            "operation_count": 7, "durable_state": "present",
            "flash_sha256": row["flash_hash"],
            "trace_sha256": row["trace_hash"],
            "uart_raw_sha256": row["uart_raw_hash"],
            "uart_semantic_sha256": row["uart_semantic_hash"],
            "mcumgr_sha256": row["mcumgr_hash"],
        }
        commit = {
            "result": "pass", "operation": cut,
            "type": self.cut_specs[cut - 1]["type"],
            "address": row["address"], "length": 4,
            "snapshot_matches_committed_bytes": True,
            "snapshot_sha256": row["fault_snapshot_hash"],
            "before_sha256": H, "after_sha256": H,
        }
        return {
            "result": "pass", "cut_point": cut,
            "matrix_row": row, "final_state_verification": run,
            "committed_operation_verification": commit,
            "bulk_inputs_verified_before_compaction": True,
        }

    def _executor(self, case_dir: Path, case: dict[str, object]) -> int:
        cut = int(case["cut"])
        repetition = int(case["repetition"])
        row = self._row(cut, repetition)
        for name, content in self._bulk_artifacts(cut, repetition).items():
            (case_dir / name).write_bytes(content)
        with (case_dir / "row.csv").open("w", newline="", encoding="utf-8") as stream:
            writer = csv.DictWriter(stream, fieldnames=MATRIX_COLUMNS, lineterminator="\n")
            writer.writeheader()
            writer.writerow(row)
        (case_dir / "evidence.json").write_text(
            json.dumps(self._compact(cut, repetition), sort_keys=True), encoding="utf-8")
        (case_dir / "verification.json").write_text(
            json.dumps(self._compact(cut, repetition)["final_state_verification"],
                       sort_keys=True), encoding="utf-8")
        (case_dir / "commit-verification.json").write_text(
            json.dumps(self._compact(cut, repetition)["committed_operation_verification"],
                       sort_keys=True), encoding="utf-8")
        attestation = {
            "schema": ATTESTATION_SCHEMA, "version": SCHEMA_VERSION,
            "proof_id": self.manifest["proof_id"], "repetition": case["repetition"],
            "worker_id": case["worker_id"],
            "cut": cut, "root_lineage_id": case["root_lineage_id"],
            "lineage_id": case["lineage_id"],
            "checkpoint_id": case["checkpoint_id"],
            "checkpoint_path": case["checkpoint_path"],
            "checkpoint_sha256": case["checkpoint_sha256"],
            "execution_mode_hash": case["execution_mode_hash"],
            "executor_id": case["executor_id"],
            "runtime_image_id": "sha256:test-x86",
            "architecture": "logical-x86", "build_identity": "worker-build-v1",
            "environment_hash": H, "offline_hash": H, "unprivileged_hash": H,
        }
        (case_dir / "worker-attestation.json").write_text(
            json.dumps(attestation, sort_keys=True), encoding="utf-8")
        (case_dir / "timing.json").write_text('{"wall_seconds": 0.25}', encoding="utf-8")
        return 0

    def _make_shards(self) -> tuple[list[dict[str, object]], Path]:
        directory = self.root / "shards"
        shards = generate_shards(self.manifest, self.lineages, self.profiles, directory, 1)
        return shards, directory

    def _artifact_metadata(
            self, cut: int, repetition: int | None = None) -> dict[str, dict[str, object]]:
        artifacts = self._bulk_artifacts(cut, repetition)
        artifacts.update({
            "row.csv": b"row", "evidence.json": b"evidence",
            "verification.json": b"verification",
            "commit-verification.json": b"commit",
        })
        return {
            name: {"sha256": sha256_bytes(content), "size": len(content)}
            for name, content in artifacts.items()
        }

    def _run_all(self, shards: list[dict[str, object]], shard_dir: Path) -> Path:
        results = self.root / "results"
        for shard in shards:
            shard_path = shard_dir / f"shard-{shard['shard_id']}.json"
            run_shard(self.manifest_path, shard_path, self.lineage_paths, results,
                      self.root / "journals" / f"{shard['shard_id']}.json", self._executor)
        return results

    def test_stable_canonical_ids_and_manifest_mismatch(self) -> None:
        reordered = dict(self.manifest)
        reordered["inputs"] = list(reversed(reordered["inputs"]))
        # One input means the canonical identity is unchanged despite object key order.
        self.assertEqual(canonical_hash(self.manifest), canonical_hash(reordered))
        self.assertEqual(self.manifest["proof_id"], create_manifest(reordered)["proof_id"])
        self.input_path.write_bytes(b"changed")
        with self.assertRaises(DistributedProofError):
            hash_declared_inputs(self.manifest, self.root)

    def test_five_unique_lineages(self) -> None:
        checked = validate_lineages(self.lineages, self.manifest)
        self.assertEqual(set(checked["by_repetition"]), set(range(1, 6)))
        duplicate = list(self.lineages)
        duplicate[-1] = dict(duplicate[0])
        with self.assertRaises(DistributedProofError):
            validate_lineages(duplicate, self.manifest)

    def test_weighted_balancing_records_phase_and_machine_cost(self) -> None:
        shards = generate_shards(self.manifest, self.lineages, self.profiles, None, 1)
        self.assertEqual(sum(len(shard["cases"]) for shard in shards), 10)
        self.assertTrue(any(case["weighted_cost"] != 1 for shard in shards for case in shard["cases"]))
        self.assertTrue(all("phase_costs" in case and "estimated_seconds" in case
                            for shard in shards for case in shard["cases"]))
        self.assertEqual({shard["worker"]["worker_id"] for shard in shards},
                         {"worker-a", "worker-b"})

    def test_one_repetition_can_use_x86_and_arm_materializations(self) -> None:
        arm_lineages: list[dict[str, object]] = []
        for source in self.lineages:
            repetition = int(source["repetition"])
            lineage = dict(source)
            lineage["architecture"] = "logical-arm64"
            lineage["build_identity"] = "worker-build-arm64-v1"
            lineage["executor_id"] = "executor-arm64"
            lineage["clean_execution_evidence_sha256"] = sha256_bytes(
                f"arm-clean-execution-{repetition}".encode())
            lineage["checkpoint_manifest_sha256"] = sha256_bytes(
                f"arm-checkpoint-manifest-{repetition}".encode())
            arm_cuts = []
            for cut in source["cuts"]:
                checkpoint_path = f"arm64/r{repetition}/c{cut['cut']}.save"
                checkpoint_file = self.checkpoint_root / checkpoint_path
                checkpoint_file.parent.mkdir(parents=True, exist_ok=True)
                checkpoint_file.write_bytes(
                    f"arm-checkpoint-{repetition}-{cut['cut']}".encode())
                checkpoint_sha256 = sha256_bytes(checkpoint_file.read_bytes())
                arm_cuts.append(dict(
                    cut,
                    checkpoint_path=checkpoint_path,
                    checkpoint_sha256=checkpoint_sha256,
                    checkpoint_id=derive_checkpoint_id(
                        source["root_lineage_id"], repetition, cut["cut"],
                        "logical-arm64", "worker-build-arm64-v1", self.mode_hash,
                        checkpoint_path, checkpoint_sha256),
                ))
            lineage["cuts"] = arm_cuts
            lineage.pop("lineage_id")
            lineage["lineage_id"] = derive_lineage_id(lineage)
            arm_lineages.append(lineage)
        pi_profile = {
            "schema": WORKER_PROFILE_SCHEMA, "version": SCHEMA_VERSION,
            "worker_id": "worker-pi", "architecture": "logical-arm64",
            "build_identity": "worker-build-arm64-v1",
            "executor_id": "executor-arm64",
            "runtime_image_id": "sha256:test-arm64",
            "cases_per_sec": 1.0, "cost_rate": 1.0,
            "phase_rates": {"boot": 2.0, "verify": 100.0},
            "attestation_hashes": {"environment_hash": H, "offline_hash": H,
                                   "unprivileged_hash": H},
        }
        shards = generate_shards(
            self.manifest, self.lineages + arm_lineages,
            self.profiles + [pi_profile], None, 1)
        all_materializations = validate_lineages(
            self.lineages + arm_lineages, self.manifest)["by_id"]
        workers_by_repetition: dict[int, set[str]] = {}
        for shard in shards:
            for case in shard["cases"]:
                workers_by_repetition.setdefault(case["repetition"], set()).add(
                    shard["worker"]["worker_id"])
                materialization = all_materializations[case["lineage_id"]]
                self.assertEqual(case["root_lineage_id"],
                                 materialization["root_lineage_id"])
        self.assertTrue(any(
            "worker-pi" in workers and workers & {"worker-a", "worker-b"}
            for workers in workers_by_repetition.values()))
        qualification = generate_qualification_shards(
            self.manifest, self.lineages + arm_lineages,
            "worker-a", "worker-pi", 1, [1, 2])
        self.assertEqual(len(qualification), 2)
        self.assertTrue(all(shard["qualification_only"] for shard in qualification))
        self.assertEqual(
            {case["cut"] for shard in qualification for case in shard["cases"]},
            {1, 2})

    def test_tampered_checkpoint_and_wrong_executor_fail_closed(self) -> None:
        inventory = verify_worker_checkpoints(
            self.manifest_path, self.root, "worker-a")
        self.assertEqual(inventory["checkpoint_artifacts"], 10)
        shards, shard_dir = self._make_shards()
        case = shards[0]["cases"][0]
        checkpoint = self.checkpoint_root / case["checkpoint_path"]
        checkpoint.write_bytes(b"substituted checkpoint")
        with self.assertRaises(DistributedProofError):
            run_shard(
                self.manifest_path,
                shard_dir / f"shard-{shards[0]['shard_id']}.json",
                self.lineage_paths, self.root / "tampered-results",
                self.root / "tampered-journal.json", self._executor)

        wrong = dict(self.lineages[0])
        wrong["executor_id"] = "executor-arm64"
        wrong.pop("lineage_id")
        wrong["lineage_id"] = derive_lineage_id(wrong)
        with self.assertRaises(DistributedProofError):
            validate_lineages([wrong] + self.lineages[1:], self.manifest)

    def test_resumable_pass_is_skipped(self) -> None:
        shards, shard_dir = self._make_shards()
        shard = shards[0]
        journal_path = self.root / "journal.json"
        run_shard(self.manifest_path, shard_dir / f"shard-{shard['shard_id']}.json",
                  self.lineage_paths, self.root / "results", journal_path, self._executor)
        def unexpected(_case_dir: Path, _case: dict[str, object]) -> int:
            raise AssertionError("PASS case was executed again")
        run_shard(self.manifest_path, shard_dir / f"shard-{shard['shard_id']}.json",
                  self.lineage_paths, self.root / "results", journal_path, unexpected)
        journal = json.loads(journal_path.read_text(encoding="utf-8"))
        self.assertTrue(all(item["status"] == "PASS" for item in journal["cases"].values()))

    def test_published_pass_survives_journal_crash_window(self) -> None:
        shards, shard_dir = self._make_shards()
        shard = shards[0]
        journal_path = self.root / "journal-crash.json"
        shard_path = shard_dir / f"shard-{shard['shard_id']}.json"
        run_shard(self.manifest_path, shard_path, self.lineage_paths,
                  self.root / "crash-results", journal_path, self._executor)
        journal = json.loads(journal_path.read_text(encoding="utf-8"))
        for state in journal["cases"].values():
            state.clear()
            state.update({"status": "RUNNING", "attempts": 1})
        journal_path.write_text(json.dumps(journal), encoding="utf-8")

        def unexpected(_case_dir: Path, _case: dict[str, object]) -> int:
            raise AssertionError("immutable PASS was executed again")

        resumed = run_shard(
            self.manifest_path, shard_path, self.lineage_paths,
            self.root / "crash-results", journal_path, unexpected)
        self.assertTrue(all(
            state["status"] == "PASS" and state.get("result_id")
            for state in resumed["cases"].values()))

    def test_atomic_partial_result_is_refused(self) -> None:
        shards, shard_dir = self._make_shards()
        shard = shards[0]
        case = shard["cases"][0]
        results = self.root / "partial-results"
        envelope = make_envelope(
            self.manifest, self.lineage_map, case,
            self._compact(case["cut"], case["repetition"]), shard["worker"],
            {"schema": ATTESTATION_SCHEMA, "version": SCHEMA_VERSION,
             "proof_id": self.manifest["proof_id"], "repetition": case["repetition"],
             "worker_id": shard["worker"]["worker_id"],
             "cut": case["cut"], "root_lineage_id": case["root_lineage_id"],
             "lineage_id": case["lineage_id"],
             "checkpoint_id": case["checkpoint_id"],
             "checkpoint_path": case["checkpoint_path"],
             "checkpoint_sha256": case["checkpoint_sha256"],
             "execution_mode_hash": case["execution_mode_hash"], "architecture": "logical-x86",
             "build_identity": "worker-build-v1", "executor_id": case["executor_id"],
             "runtime_image_id": "sha256:test-x86",
             "environment_hash": H,
             "offline_hash": H, "unprivileged_hash": H},
            self._artifact_metadata(case["cut"], case["repetition"]),
            {"wall_seconds": 1}, shard["shard_id"],
        )
        path = result_path(results, envelope)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"schema":', encoding="utf-8")
        with self.assertRaises(DistributedProofError):
            aggregate_results(self.manifest_path, self.lineage_paths,
                              [shard_dir / f"shard-{shard['shard_id']}.json"], results,
                              self.root / "out")

    def test_compact_evidence_rejects_matrix_hash_divergence(self) -> None:
        compact = self._compact(1)
        compact["matrix_row"]["flash_hash"] = "b" * 64
        with self.assertRaises(DistributedProofError):
            validate_compact_evidence(compact, 1)

    def test_duplicate_and_stale_result_rejection(self) -> None:
        shards, shard_dir = self._make_shards()
        results = self._run_all(shards, shard_dir)
        first = next(results.rglob("result-*.json"))
        duplicate = json.loads(first.read_text(encoding="utf-8"))
        duplicate["timing"] = {"wall_seconds": 99}
        duplicate["result_id"] = canonical_hash({key: value for key, value in duplicate.items() if key != "result_id"})
        duplicate_path = first.parent / f"result-r{duplicate['repetition']}-c{duplicate['cut']}-{duplicate['result_id']}.json"
        atomic_publish_result(duplicate_path, duplicate)
        with self.assertRaises(DistributedProofError):
            aggregate_results(self.manifest_path, self.lineage_paths,
                              [self.root / "shards" / path.name for path in []] or
                              sorted((self.root / "shards").glob("shard-*.json")),
                              results, self.root / "rejected")

        stale_root = self.root / "stale-results"
        stale = json.loads(first.read_text(encoding="utf-8"))
        stale["proof_id"] = "b" * 64
        stale["result_id"] = canonical_hash(
            {key: value for key, value in stale.items() if key != "result_id"})
        stale_path = result_path(stale_root, stale)
        atomic_publish_result(stale_path, stale)
        with self.assertRaises(DistributedProofError):
            aggregate_results(
                self.manifest_path, self.lineage_paths,
                sorted((self.root / "shards").glob("shard-*.json")),
                stale_root, self.root / "stale-rejected")

    def test_incomplete_aggregation_refuses_to_emit_summary(self) -> None:
        shards, shard_dir = self._make_shards()
        with self.assertRaises(DistributedProofError):
            aggregate_results(self.manifest_path, self.lineage_paths,
                              sorted(shard_dir.glob("shard-*.json")), self.root / "empty-results",
                              self.root / "out")
        self.assertFalse((self.root / "out" / "distributed-matrix-summary.json").exists())

    def test_successful_five_repetition_aggregation_uses_existing_verifier(self) -> None:
        shards, shard_dir = self._make_shards()
        results = self._run_all(shards, shard_dir)
        summary = aggregate_results(self.manifest_path, self.lineage_paths,
                                    sorted(shard_dir.glob("shard-*.json")), results,
                                    self.root / "aggregate")
        self.assertEqual(summary["result"], "pass")
        self.assertEqual(summary["repetitions"], 5)
        determinism = json.loads(
            (self.root / "aggregate" / "determinism-summary.json").read_text(
                encoding="utf-8"))
        self.assertEqual(determinism["deterministic_outcomes"], 10)
        validated = validate_aggregation_summary(
            self.manifest_path, self.root, shard_dir,
            self.root / "aggregate" / "determinism-summary.json")
        self.assertEqual(validated["result"], "pass")
        self.assertFalse((self.root / "aggregate" / "proof-summary.json").exists())
        self.assertFalse("PROVEN" in json.dumps(summary))

    def test_staged_transfer_promotes_only_valid_worker_objects(self) -> None:
        deployment = self.root / "deployment"
        init_layout(deployment)
        shards, shard_dir = self._make_shards()
        outgoing = deployment / "transfer" / "outgoing"
        selected = stage_worker_shards(
            self.manifest_path, self.root, shard_dir, outgoing, "worker-a")
        self.assertGreater(selected["selected"], 0)
        (outgoing / "shard-interrupted.json.tmp").write_text(
            '{"partial":', encoding="utf-8")
        installed = install_shards(
            self.manifest_path, self.root, outgoing,
            deployment / "shards" / "ready", "worker-a")
        self.assertEqual(installed["installed"], selected["selected"])
        for shard in shards:
            if shard["worker"]["worker_id"] == "worker-a":
                self.assertTrue((deployment / "shards" / "ready"
                                 / f"shard-{shard['shard_id']}.json").is_file())
            else:
                self.assertFalse((deployment / "shards" / "ready"
                                  / f"shard-{shard['shard_id']}.json").exists())

    def test_collected_results_are_revalidated_before_promotion(self) -> None:
        shards, shard_dir = self._make_shards()
        remote_results = self._run_all(shards, shard_dir)
        central_results = self.root / "central-results"
        collected = install_results(
            self.manifest_path, self.root, shard_dir,
            remote_results, central_results)
        self.assertEqual(collected["installed"], 10)
        again = install_results(
            self.manifest_path, self.root, shard_dir,
            remote_results, central_results)
        self.assertEqual(again, {"installed": 0, "existing": 10})
        summary = deployment_status(
            self.manifest_path, self.root, shard_dir, central_results)
        self.assertEqual(summary["pass"], 10)
        self.assertEqual(summary["pending"], 0)

    def test_benchmark_rejects_throttled_count_and_uses_measured_rates(self) -> None:
        harness = self.root / "benchmark_harness.py"
        harness.write_text(
            "import argparse, json\n"
            "from pathlib import Path\n"
            "p=argparse.ArgumentParser(); p.add_argument('--phase'); "
            "p.add_argument('--workers',type=int); p.add_argument('--output')\n"
            "a=p.parse_args(); cases=6; rate=a.workers*(1 if a.phase=='upload' else 2)\n"
            "Path(a.output,'metrics.json').write_text(json.dumps({"
            "'case_count':cases,'wall_seconds':cases/rate,'cpu_percent':95*a.workers,"
            "'peak_rss_bytes':1000000*a.workers,'max_fds':20*a.workers,"
            "'temperature_c':50+a.workers,'throttled':a.workers==4,"
            "'memory_pressure':False,'descriptor_leak':False,'stable':True}))\n",
            encoding="utf-8")
        result = benchmark(
            "worker-a", "logical-x86", "worker-build-v1",
            [sys.executable, str(harness), "--phase", "{phase}",
             "--workers", "{workers}", "--output", "{output}"],
            [1, 2, 3, 4], 3, self.root / "benchmark-runs", 10165, 20544)
        self.assertEqual(result["recommended_workers"], 3)
        self.assertEqual(len(result["measurements"]), 4)
        self.assertFalse(result["measurements"][-1]["eligible"])

    def test_existing_finalizer_binds_distributed_gate_proof_id(self) -> None:
        app = self.root / "finalizer-app"
        artifacts = app / "artifacts"
        paths = {
            "baseline": artifacts / "baseline" / "baseline-summary.json",
            "determinism": artifacts / "determinism-summary.json",
            "negative": artifacts / "negative-tests" / "negative-tests-summary.json",
            "unprivileged": artifacts / "unprivileged" / "unprivileged-summary.json",
            "hook": artifacts / "baseline" / "fault-hook" / "fault-hook-summary.json",
            "fixture_baseline": artifacts / "baseline" / "fixture-verification.json",
            "fixture_matrix": artifacts / "fixture-verification.json",
            "fixture_negative": artifacts / "negative-tests" / "fixture-verification.json",
        }
        for path in paths.values():
            path.parent.mkdir(parents=True, exist_ok=True)
        common = {"result": "pass", "proof_id": H}
        paths["baseline"].write_text(json.dumps(common), encoding="utf-8")
        paths["negative"].write_text(json.dumps(common), encoding="utf-8")
        paths["unprivileged"].write_text(json.dumps(common), encoding="utf-8")
        paths["hook"].write_text(json.dumps(common), encoding="utf-8")
        determinism = dict(common, repetitions=5, cut_points=2,
                           deterministic_outcomes=10)
        paths["determinism"].write_text(json.dumps(determinism), encoding="utf-8")
        fixture = dict(common, signed_fixture_hashes={"v1": H})
        for name in ("fixture_baseline", "fixture_matrix", "fixture_negative"):
            paths[name].write_text(json.dumps(fixture), encoding="utf-8")
        environment = dict(os.environ, APP_ROOT=str(app), DISTRIBUTED_PROOF_ID=H)
        completed = subprocess.run(
            [sys.executable, str(Path(__file__).parent / "finalize_proof.py")],
            env=environment, capture_output=True, text=True, check=False)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        summary = json.loads(
            (artifacts / "proof-summary.json").read_text(encoding="utf-8"))
        self.assertEqual(summary["verdict"], "PROVEN")
        self.assertEqual(summary["proof_id"], H)

        paths["baseline"].write_text(
            json.dumps({"result": "pass", "proof_id": "b" * 64}),
            encoding="utf-8")
        rejected = subprocess.run(
            [sys.executable, str(Path(__file__).parent / "finalize_proof.py")],
            env=environment, capture_output=True, text=True, check=False)
        self.assertNotEqual(rejected.returncode, 0)


if __name__ == "__main__":
    unittest.main()
