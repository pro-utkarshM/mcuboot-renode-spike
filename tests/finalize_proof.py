#!/usr/bin/env python3
"""Aggregate only already-verified acceptance outputs into the final verdict."""

import json
import os
from pathlib import Path

root = Path(os.environ.get("APP_ROOT", "/workspace/app"))
artifacts = root / "artifacts"
distributed_proof_id = os.environ.get("DISTRIBUTED_PROOF_ID")
required = {
    "baseline": artifacts / "baseline" / "baseline-summary.json",
    "determinism": artifacts / "determinism-summary.json",
    "negative_tests": artifacts / "negative-tests" / "negative-tests-summary.json",
    "unprivileged": artifacts / "unprivileged" / "unprivileged-summary.json",
}
results = {}
for name, path in required.items():
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("result") != "pass":
        raise SystemExit(f"{name} did not pass: {path}")
    if distributed_proof_id and payload.get("proof_id") != distributed_proof_id:
        raise SystemExit(f"{name} does not belong to distributed proof {distributed_proof_id}")
    results[name] = payload

fixture_required = {
    "baseline": artifacts / "baseline" / "fixture-verification.json",
    "matrix": artifacts / "fixture-verification.json",
    "negative_tests":
        artifacts / "negative-tests" / "fixture-verification.json",
}
fixture_results = {}
for name, path in fixture_required.items():
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("result") != "pass":
        raise SystemExit(f"{name} fixture verification did not pass: {path}")
    if distributed_proof_id and payload.get("proof_id") != distributed_proof_id:
        raise SystemExit(
            f"{name} fixtures do not belong to distributed proof {distributed_proof_id}")
    fixture_results[name] = payload
first_fixture = next(iter(fixture_results.values()))
for name, payload in fixture_results.items():
    if payload != first_fixture:
        raise SystemExit(
            f"{name} fixture verification does not match the baseline fixtures")

determinism = results["determinism"]
if determinism.get("repetitions", 0) < 5:
    raise SystemExit("determinism proof has fewer than five repetitions")
if determinism.get("cut_points", 0) < 1:
    raise SystemExit("determinism proof has no cut points")
if determinism.get("deterministic_outcomes") != (
        determinism["cut_points"] * determinism["repetitions"]):
    raise SystemExit("determinism outcome total is inconsistent")
hook = json.loads((artifacts / "baseline" / "fault-hook" /
                   "fault-hook-summary.json").read_text(encoding="utf-8"))
if hook.get("result") != "pass":
    raise SystemExit("fault-hook proof did not pass")
if distributed_proof_id and hook.get("proof_id") != distributed_proof_id:
    raise SystemExit("fault-hook proof belongs to another distributed proof")
results["fault_hook"] = hook
summary = {
    "verdict": "PROVEN",
    "all_acceptance_conditions_passed": True,
    "cut_points": determinism["cut_points"],
    "repetitions": determinism["repetitions"],
    "fault_injected_runs": determinism["deterministic_outcomes"],
    "deterministic_outcomes": determinism["deterministic_outcomes"],
    "hangs": 0,
    "unrecoverable_states": 0,
    "fixture_verification": fixture_results,
    "gates": results,
}
if distributed_proof_id:
    summary["proof_id"] = distributed_proof_id
summary_path = artifacts / "proof-summary.json"
temporary_path = artifacts / ".proof-summary.json.tmp"
temporary_path.write_text(
    json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
os.replace(temporary_path, summary_path)
print(json.dumps(summary, indent=2, sort_keys=True))
