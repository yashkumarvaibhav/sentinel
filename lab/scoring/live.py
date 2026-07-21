"""Contained k6 workload orchestration and real ClickHouse evidence extraction."""

from __future__ import annotations

import json
import math
import re
import subprocess
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from lab.scenarios import ScenarioArtifacts, schedule_payload
from lab.scenarios.compiler import CompiledSchedule

_SAFE_NAME = re.compile(r"^[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?$")
_NAMESPACE = "otel-demo"
_CONTEXT = "k3d-sentinel-lab"


@dataclass(frozen=True)
class LiveTelemetry:
    start_at: datetime
    span_timestamps: tuple[datetime, ...]


def expected_request_count(schedule: CompiledSchedule) -> int:
    return sum(phase.rate_rps * phase.duration_seconds for phase in schedule.phases)


def build_k6_job(
    schedule: CompiledSchedule,
    *,
    job_name: str,
    run_id: str,
) -> dict[str, Any]:
    _require_safe(job_name, "job_name")
    _require_safe(run_id, "run_id")
    encoded_schedule = json.dumps(
        schedule_payload(schedule), allow_nan=False, separators=(",", ":"), sort_keys=True
    )
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {
            "name": job_name,
            "namespace": _NAMESPACE,
            "labels": {"sentinel.dev/role": "loadgen"},
        },
        "spec": {
            "backoffLimit": 0,
            "ttlSecondsAfterFinished": 600,
            "template": {
                "metadata": {"labels": {"sentinel.dev/role": "loadgen"}},
                "spec": {
                    "restartPolicy": "Never",
                    "containers": [
                        {
                            "name": "k6",
                            "image": "grafana/k6:0.55.0",
                            "args": ["run", "/scripts/scenario.js"],
                            "env": [
                                {"name": "SENTINEL_SCHEDULE", "value": encoded_schedule},
                                {"name": "SENTINEL_RUN_ID", "value": run_id},
                            ],
                            "resources": {
                                "limits": {"cpu": "1", "memory": "256Mi"},
                                "requests": {"cpu": "100m", "memory": "64Mi"},
                            },
                            "volumeMounts": [{"name": "script", "mountPath": "/scripts"}],
                        }
                    ],
                    "volumes": [{"name": "script", "configMap": {"name": job_name}}],
                },
            },
        },
    }


def run_live_scenario(
    *,
    repo_root: Path,
    artifacts: ScenarioArtifacts,
    invocation: str,
) -> LiveTelemetry:
    _require_safe(invocation, "invocation")
    schedule = artifacts.schedule
    stem = f"{schedule.scenario_id.replace('_', '-')}-{schedule.seed}-{invocation}"
    job_name = f"score-{stem}"
    run_id = f"run-{stem}"
    job = build_k6_job(schedule, job_name=job_name, run_id=run_id)
    script = repo_root / "lab" / "loadgen" / "scenario.js"
    _preflight(repo_root)
    _delete_owned(job_name)
    try:
        _run(
            [
                "kubectl",
                "--context",
                _CONTEXT,
                "-n",
                _NAMESPACE,
                "create",
                "configmap",
                job_name,
                f"--from-file=scenario.js={script}",
            ]
        )
        _run(
            ["kubectl", "--context", _CONTEXT, "apply", "-f", "-"],
            input_text=yaml.safe_dump(job, sort_keys=False),
        )
        duration = sum(phase.duration_seconds for phase in schedule.phases)
        _wait_for_job(job_name, timeout_seconds=duration + 180)
        log = _run(["kubectl", "--context", _CONTEXT, "-n", _NAMESPACE, "logs", f"job/{job_name}"])
        if "iterations" not in log or "dropped_iterations" not in log:
            raise RuntimeError(f"k6 completion log lacked workload evidence for {job_name}")
        timestamps = _wait_for_spans(
            repo_root=repo_root,
            user_agent=f"sentinel-score/{run_id}",
            expected=expected_request_count(schedule),
        )
        start_at = _wait_for_anchor(
            repo_root=repo_root,
            user_agent=f"sentinel-score-anchor/{run_id}",
        )
        return LiveTelemetry(start_at=start_at, span_timestamps=timestamps)
    finally:
        _delete_owned(job_name)


def _preflight(repo_root: Path) -> None:
    _run(["kubectl", "--context", _CONTEXT, "get", "namespace", _NAMESPACE])
    _run(
        [
            "docker",
            "compose",
            "--project-name",
            "sentinel",
            "--project-directory",
            str(repo_root),
            "-f",
            str(repo_root / "deploy" / "docker-compose.yml"),
            "exec",
            "-T",
            "clickhouse",
            "clickhouse-client",
            "--query",
            "SELECT 1",
        ]
    )


def _wait_for_spans(
    *,
    repo_root: Path,
    user_agent: str,
    expected: int,
) -> tuple[datetime, ...]:
    deadline = time.monotonic() + 45
    latest: tuple[datetime, ...] = ()
    previous_count = -1
    stable_polls = 0
    completeness_count = math.ceil(expected * 0.95)
    while time.monotonic() < deadline:
        latest = _query_spans(repo_root=repo_root, user_agent=user_agent)
        if len(latest) >= expected:
            return latest
        if len(latest) == previous_count:
            stable_polls += 1
        else:
            previous_count = len(latest)
            stable_polls = 0
        if len(latest) >= completeness_count and stable_polls >= 3:
            return latest
        time.sleep(2)
    if latest:
        return latest
    raise RuntimeError(f"no ingress spans arrived for {user_agent}")


def _wait_for_job(name: str, *, timeout_seconds: int) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        document = json.loads(
            _run(
                [
                    "kubectl",
                    "--context",
                    _CONTEXT,
                    "-n",
                    _NAMESPACE,
                    "get",
                    "job",
                    name,
                    "-o",
                    "json",
                ]
            )
        )
        status = document.get("status", {})
        if isinstance(status, dict) and status.get("succeeded") == 1:
            return
        if isinstance(status, dict) and status.get("failed", 0) >= 1:
            log = _run(["kubectl", "--context", _CONTEXT, "-n", _NAMESPACE, "logs", f"job/{name}"])
            raise RuntimeError(f"k6 job failed: {name}\n{log}")
        time.sleep(2)
    raise RuntimeError(f"k6 job timed out: {name}")


def _wait_for_anchor(*, repo_root: Path, user_agent: str) -> datetime:
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        timestamps = _query_spans(repo_root=repo_root, user_agent=user_agent)
        if timestamps:
            return timestamps[-1]
        time.sleep(2)
    raise RuntimeError(f"scenario start marker did not arrive for {user_agent}")


def _query_spans(*, repo_root: Path, user_agent: str) -> tuple[datetime, ...]:
    query = """
        SELECT toUnixTimestamp64Micro(ts) AS ts_us
        FROM sentinel.observations FINAL
        WHERE service = 'frontend-proxy'
          AND signal = 'span.duration_ms'
          AND JSONExtractInt(attributes_json, 'span.kind') = 2
          AND JSONExtractString(attributes_json, 'user_agent') = {user_agent:String}
        ORDER BY ts, observation_id
        FORMAT JSONEachRow
    """
    output = _run(
        [
            "docker",
            "compose",
            "--project-name",
            "sentinel",
            "--project-directory",
            str(repo_root),
            "-f",
            str(repo_root / "deploy" / "docker-compose.yml"),
            "exec",
            "-T",
            "clickhouse",
            "clickhouse-client",
            f"--param_user_agent={user_agent}",
            "--query",
            query,
        ]
    )
    records = [json.loads(line) for line in output.splitlines() if line]
    return tuple(
        datetime.fromtimestamp(int(record["ts_us"]) / 1_000_000, tz=UTC) for record in records
    )


def _delete_owned(name: str) -> None:
    _require_safe(name, "resource name")
    _run(
        [
            "kubectl",
            "--context",
            _CONTEXT,
            "-n",
            _NAMESPACE,
            "delete",
            "job",
            name,
            "--ignore-not-found",
            "--wait=true",
        ]
    )
    _run(
        [
            "kubectl",
            "--context",
            _CONTEXT,
            "-n",
            _NAMESPACE,
            "delete",
            "configmap",
            name,
            "--ignore-not-found",
            "--wait=true",
        ]
    )


def _run(
    command: list[str],
    *,
    input_text: str | None = None,
    timeout: int = 120,
) -> str:
    completed = subprocess.run(
        command,
        input=input_text,
        text=True,
        capture_output=True,
        check=False,
        timeout=timeout,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise RuntimeError(f"command failed ({command[0]}): {detail}")
    return completed.stdout


def _require_safe(value: str, field: str) -> None:
    if _SAFE_NAME.fullmatch(value) is None:
        raise ValueError(f"unsafe {field}: {value!r}")
