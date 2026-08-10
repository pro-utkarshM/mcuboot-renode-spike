#!/usr/bin/env python3
"""Run a sealed benchmark harness and select the measured worker-count optimum."""

from __future__ import annotations

import argparse
import json
import shlex
import statistics
import subprocess
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

try:
    from .manifest import (
        DistributedProofError,
        canonical_hash,
        load_manifest,
        write_immutable_json,
    )
except ImportError:  # pragma: no cover
    from manifest import (  # type: ignore
        DistributedProofError, canonical_hash, load_manifest, write_immutable_json)


REQUIRED_METRICS = (
    "case_count", "wall_seconds", "cpu_percent", "peak_rss_bytes",
    "max_fds", "stable", "throttled", "memory_pressure",
    "descriptor_leak",
)


def _metric(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        raise DistributedProofError(f"benchmark metric {name} is invalid")
    return float(value)


def _run_sample(
    command: Sequence[str], phase: str, workers: int, sample: int,
    output_dir: Path,
) -> dict[str, Any]:
    sample_dir = output_dir / f"workers-{workers}" / phase / f"sample-{sample}"
    sample_dir.mkdir(parents=True, exist_ok=False)
    values = {
        "phase": phase,
        "workers": str(workers),
        "sample": str(sample),
        "output": str(sample_dir),
    }
    args = [part.format(**values) for part in command]
    completed = subprocess.run(
        args, cwd=sample_dir, text=True, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, check=False)
    (sample_dir / "benchmark.log").write_text(
        completed.stdout, encoding="utf-8")
    if completed.returncode != 0:
        raise DistributedProofError(
            f"benchmark harness failed for {phase}/{workers}/{sample}: "
            f"status {completed.returncode}")
    metrics_path = sample_dir / "metrics.json"
    try:
        metrics = json.loads(metrics_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DistributedProofError(
            f"benchmark harness did not produce valid {metrics_path}: {exc}") from exc
    if not isinstance(metrics, Mapping):
        raise DistributedProofError("benchmark metrics must be a JSON object")
    missing = sorted(set(REQUIRED_METRICS) - set(metrics))
    if missing:
        raise DistributedProofError(
            "benchmark metrics are incomplete: " + ", ".join(missing))
    case_count = _metric(metrics["case_count"], "case_count")
    wall_seconds = _metric(metrics["wall_seconds"], "wall_seconds")
    if case_count <= 0 or wall_seconds <= 0 or metrics["stable"] is not True:
        raise DistributedProofError("benchmark sample is empty or unstable")
    result = dict(metrics)
    for field in ("cpu_percent", "peak_rss_bytes", "max_fds"):
        _metric(result[field], field)
    for field in ("throttled", "memory_pressure", "descriptor_leak"):
        if result.get(field) not in (False, True):
            raise DistributedProofError(
                f"benchmark {field} field must be boolean")
    result["cases_per_second"] = case_count / wall_seconds
    result["seconds_per_case"] = wall_seconds / case_count
    return result


def benchmark(
    worker_id: str,
    architecture: str,
    build_identity: str,
    command: Sequence[str],
    worker_counts: Sequence[int],
    samples: int,
    output_dir: Path,
    upload_cuts: int,
    swap_cuts: int,
) -> dict[str, Any]:
    if samples < 3:
        raise DistributedProofError("at least three benchmark samples are required")
    if not worker_counts or any(value < 1 for value in worker_counts):
        raise DistributedProofError("worker counts must be positive")
    measurements = []
    for workers in worker_counts:
        phases: dict[str, Any] = {}
        for phase in ("upload", "swap"):
            runs = [_run_sample(
                command, phase, workers, sample, output_dir)
                    for sample in range(1, samples + 1)]
            phases[phase] = {
                "samples": runs,
                "median_seconds_per_case": statistics.median(
                    run["seconds_per_case"] for run in runs),
                "cases_per_second": sum(run["case_count"] for run in runs)
                                    / sum(run["wall_seconds"] for run in runs),
                "peak_rss_bytes": max(run["peak_rss_bytes"] for run in runs),
                "max_fds": max(run["max_fds"] for run in runs),
                "median_cpu_percent": statistics.median(
                    run["cpu_percent"] for run in runs),
                "max_temperature_c": max(
                    (run.get("temperature_c", 0.0) for run in runs), default=0.0),
                "throttled": any(run.get("throttled") is True for run in runs),
                "memory_pressure": any(
                    run.get("memory_pressure") is True for run in runs),
                "descriptor_leak": any(
                    run.get("descriptor_leak") is True for run in runs),
            }
        projected_seconds = (
            upload_cuts / phases["upload"]["cases_per_second"]
            + swap_cuts / phases["swap"]["cases_per_second"])
        measurements.append({
            "workers": workers,
            "phases": phases,
            "projected_repetition_seconds": projected_seconds,
            "eligible": not any(
                phases[phase][field]
                for phase in ("upload", "swap")
                for field in ("throttled", "memory_pressure", "descriptor_leak")),
        })
    eligible = [item for item in measurements if item["eligible"]]
    if not eligible:
        raise DistributedProofError(
            "all worker counts throttled; refusing a recommendation")
    recommended = min(
        eligible, key=lambda item: (
            item["projected_repetition_seconds"], item["workers"]))
    payload: dict[str, Any] = {
        "schema": "ota.distributed-proof.worker-benchmark",
        "version": 1,
        "worker_id": worker_id,
        "architecture": architecture,
        "build_identity": build_identity,
        "command_sha256": canonical_hash(list(command)),
        "samples_per_phase": samples,
        "upload_cuts": upload_cuts,
        "swap_cuts": swap_cuts,
        "measurements": measurements,
        "recommended_workers": recommended["workers"],
        "recommended_phase_rates": {
            phase: recommended["phases"][phase]["cases_per_second"]
            for phase in ("upload", "swap")
        },
        "result": "pass",
    }
    payload["benchmark_id"] = canonical_hash(payload)
    return payload


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker-id", required=True)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--executor-id", required=True)
    parser.add_argument("--architecture", required=True)
    parser.add_argument("--build-identity", required=True)
    parser.add_argument("--command", required=True)
    parser.add_argument("--worker-counts", default="1,2,3,4")
    parser.add_argument("--samples", type=int, default=3)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--summary", required=True, type=Path)
    parser.add_argument("--upload-cuts", type=int, default=10165)
    parser.add_argument("--swap-cuts", type=int, default=20544)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        command = shlex.split(args.command)
        manifest = load_manifest(args.manifest)
        worker = manifest["qualified_workers"].get(args.worker_id)
        executor = manifest["executors"].get(args.executor_id)
        if (not isinstance(worker, Mapping) or not isinstance(executor, Mapping)
                or worker.get("executor_id") != args.executor_id
                or worker.get("architecture") != args.architecture
                or worker.get("build_identity") != args.build_identity
                or executor.get("benchmark_argv_sha256") != canonical_hash(command)):
            raise DistributedProofError(
                "benchmark harness or worker identity is not frozen by the manifest")
        counts = tuple(int(value) for value in args.worker_counts.split(",") if value)
        result = benchmark(
            args.worker_id, args.architecture, args.build_identity,
            command, counts, args.samples, args.output_dir,
            args.upload_cuts, args.swap_cuts)
        result["proof_id"] = manifest["proof_id"]
        result["executor_id"] = args.executor_id
        result.pop("benchmark_id", None)
        result["benchmark_id"] = canonical_hash(result)
        write_immutable_json(args.summary, result)
        print(json.dumps({
            "result": "pass",
            "recommended_workers": result["recommended_workers"],
            "benchmark_id": result["benchmark_id"],
        }, sort_keys=True))
        return 0
    except (DistributedProofError, OSError, ValueError) as exc:
        print(f"worker benchmark failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
