"""Contained k6 workload orchestration and real ClickHouse evidence extraction."""

from __future__ import annotations

import hashlib
import json
import math
import re
import subprocess
import time
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, Protocol, cast

import yaml

from lab.scenarios import ScenarioArtifacts, schedule_payload
from lab.scenarios.compiler import CompiledSchedule
from lab.scenarios.models import (
    ChaosMeshStimulus,
    FlagdStimulus,
    K6JourneyStimulus,
    Stimulus,
    stimulus_target,
)

_SAFE_NAME = re.compile(r"^[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?$")
_NAMESPACE = "otel-demo"
_CONTEXT = "k3d-sentinel-lab"
_FLAG_CONFIG_MAP = "flagd-config"
_FLAG_DEPLOYMENT = "flagd"
_FLAG_DOCUMENT_KEY = "demo.flagd.json"
_ALLOWED_CHAOS_KINDS = frozenset({"StressChaos", "NetworkChaos", "PodChaos", "IOChaos"})
_MAX_CHAOS_DURATION_SECONDS = 300


class CommandRunner(Protocol):
    def __call__(
        self,
        command: list[str],
        *,
        input_text: str | None = None,
        timeout: int = 120,
    ) -> str: ...


@dataclass(frozen=True)
class LiveTelemetry:
    start_at: datetime
    span_timestamps: tuple[datetime, ...]
    correlation_user_agent: str
    anchor_user_agent: str
    stimulus_executions: tuple[StimulusExecution, ...]


@dataclass(frozen=True)
class StimulusExecution:
    stimulus_id: str
    kind: Literal["flagd", "chaos_mesh", "k6_journey"]
    target: str
    setting: str
    requested_start_offset_seconds: int
    requested_end_offset_seconds: int
    started_at: datetime
    ended_at: datetime

    def canonical_value(self) -> dict[str, object]:
        return {
            "ended_at": self.ended_at.isoformat(),
            "kind": self.kind,
            "requested_end_offset_seconds": self.requested_end_offset_seconds,
            "requested_start_offset_seconds": self.requested_start_offset_seconds,
            "setting": self.setting,
            "started_at": self.started_at.isoformat(),
            "stimulus_id": self.stimulus_id,
            "target": self.target,
        }


@dataclass(frozen=True)
class _StimulusTransition:
    offset_seconds: int
    operation: Literal["start", "stop"]
    stimulus: Stimulus


@dataclass(frozen=True)
class _ChaosManifest:
    path: Path
    kind: str
    name: str


@dataclass(frozen=True)
class _JourneyRun:
    job_name: str
    user_agent: str
    expected_spans: int
    timeout_seconds: int


def render_stimulus_executions(executions: tuple[StimulusExecution, ...]) -> bytes:
    value = {
        "items": [
            item.canonical_value() for item in sorted(executions, key=lambda item: item.stimulus_id)
        ],
        "version": 1,
    }
    return (
        json.dumps(value, allow_nan=False, separators=(",", ":"), sort_keys=True) + "\n"
    ).encode()


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


def build_checkout_journey_job(
    stimulus: K6JourneyStimulus,
    *,
    job_name: str,
    run_id: str,
) -> dict[str, Any]:
    _require_safe(job_name, "job_name")
    _require_safe(run_id, "run_id")
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
                                {"name": "SENTINEL_JOURNEY", "value": stimulus.journey},
                                {"name": "SENTINEL_RATE_RPS", "value": str(stimulus.rate_rps)},
                                {
                                    "name": "SENTINEL_DURATION_SECONDS",
                                    "value": str(stimulus.duration_seconds),
                                },
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
        if schedule.stimuli:
            start_at = _wait_for_anchor(
                repo_root=repo_root,
                user_agent=f"sentinel-score-anchor/{run_id}",
            )
            stimulus_executions = execute_stimuli(
                repo_root=repo_root,
                schedule=schedule,
                anchor_ts=start_at,
                invocation=invocation,
            )
        else:
            start_at = None
            stimulus_executions = ()
        _wait_for_job(job_name, timeout_seconds=duration + 180)
        log = _run(["kubectl", "--context", _CONTEXT, "-n", _NAMESPACE, "logs", f"job/{job_name}"])
        if "iterations" not in log or "dropped_iterations" not in log:
            raise RuntimeError(f"k6 completion log lacked workload evidence for {job_name}")
        timestamps = _wait_for_spans(
            repo_root=repo_root,
            user_agent=f"sentinel-score/{run_id}",
            expected=expected_request_count(schedule),
        )
        if start_at is None:
            start_at = _wait_for_anchor(
                repo_root=repo_root,
                user_agent=f"sentinel-score-anchor/{run_id}",
            )
        return LiveTelemetry(
            start_at=start_at,
            span_timestamps=timestamps,
            correlation_user_agent=f"sentinel-score/{run_id}",
            anchor_user_agent=f"sentinel-score-anchor/{run_id}",
            stimulus_executions=stimulus_executions,
        )
    finally:
        _delete_owned(job_name)


def execute_stimuli(
    *,
    repo_root: Path,
    schedule: CompiledSchedule,
    anchor_ts: datetime,
    invocation: str = "manual",
    runner: CommandRunner | None = None,
    waiter: Callable[[datetime], None] | None = None,
    clock: Callable[[], datetime] | None = None,
    journey_span_reader: Callable[[str, int], tuple[datetime, ...]] | None = None,
) -> tuple[StimulusExecution, ...]:
    """Run only compiled, contained lab stimuli and restore every changed target."""
    if anchor_ts.tzinfo is None or anchor_ts.utcoffset() != timedelta(0):
        raise ValueError("stimulus anchor must be timezone-aware UTC")
    _require_safe(invocation, "invocation")
    command = _run if runner is None else runner
    wait_until = _wait_until if waiter is None else waiter
    now = _utc_now if clock is None else clock
    read_journey_spans = (
        (
            lambda user_agent, expected: _wait_for_spans(
                repo_root=repo_root,
                user_agent=user_agent,
                expected=expected,
            )
        )
        if journey_span_reader is None
        else journey_span_reader
    )
    if not schedule.stimuli:
        return ()

    chaos = {
        stimulus.stimulus_id: _load_chaos_manifest(repo_root, stimulus)
        for stimulus in schedule.stimuli
        if isinstance(stimulus, ChaosMeshStimulus)
    }
    flag_stimuli = tuple(
        stimulus for stimulus in schedule.stimuli if isinstance(stimulus, FlagdStimulus)
    )
    flags = _FlagdController(command, flag_stimuli) if flag_stimuli else None
    active_chaos: dict[str, _ChaosManifest] = {}
    active_journeys: dict[str, _JourneyRun] = {}
    started: dict[str, datetime] = {}
    completed: list[StimulusExecution] = []
    primary_error: BaseException | None = None

    try:
        transitions = _stimulus_transitions(schedule.stimuli)
        offsets = sorted({transition.offset_seconds for transition in transitions})
        for offset in offsets:
            wait_until(anchor_ts + timedelta(seconds=offset))
            for transition in (item for item in transitions if item.offset_seconds == offset):
                stimulus = transition.stimulus
                if transition.operation == "start":
                    if isinstance(stimulus, FlagdStimulus):
                        if flags is None:  # pragma: no cover - guarded by construction
                            raise AssertionError("flagd controller missing")
                        flags.activate(stimulus)
                    elif isinstance(stimulus, ChaosMeshStimulus):
                        manifest = chaos[stimulus.stimulus_id]
                        active_chaos[stimulus.stimulus_id] = manifest
                        _apply_chaos(command, manifest)
                    else:
                        journey = _start_checkout_journey(
                            command,
                            repo_root=repo_root,
                            schedule=schedule,
                            stimulus=stimulus,
                            invocation=invocation,
                        )
                        active_journeys[stimulus.stimulus_id] = journey
                    started[stimulus.stimulus_id] = _require_utc(now(), "stimulus start")
                    continue

                if isinstance(stimulus, FlagdStimulus):
                    if flags is None:  # pragma: no cover - guarded by construction
                        raise AssertionError("flagd controller missing")
                    flags.deactivate(stimulus)
                    started_at = started.pop(stimulus.stimulus_id)
                    ended_at = _require_utc(now(), "stimulus end")
                elif isinstance(stimulus, ChaosMeshStimulus):
                    manifest = active_chaos[stimulus.stimulus_id]
                    _delete_chaos(command, manifest)
                    del active_chaos[stimulus.stimulus_id]
                    started_at = started.pop(stimulus.stimulus_id)
                    ended_at = _require_utc(now(), "stimulus end")
                else:
                    journey = active_journeys[stimulus.stimulus_id]
                    timestamps = _finish_checkout_journey(
                        command,
                        journey=journey,
                        span_reader=read_journey_spans,
                    )
                    del active_journeys[stimulus.stimulus_id]
                    started.pop(stimulus.stimulus_id)
                    started_at = timestamps[0]
                    ended_at = timestamps[-1] + timedelta(microseconds=1)
                completed.append(
                    StimulusExecution(
                        stimulus_id=stimulus.stimulus_id,
                        kind=stimulus.kind,
                        target=stimulus_target(stimulus),
                        setting=_stimulus_setting(stimulus),
                        requested_start_offset_seconds=stimulus.start_offset_seconds,
                        requested_end_offset_seconds=(
                            stimulus.start_offset_seconds + stimulus.duration_seconds
                        ),
                        started_at=started_at,
                        ended_at=ended_at,
                    )
                )
    except BaseException as exc:
        primary_error = exc

    cleanup_errors: list[Exception] = []
    for manifest in reversed(tuple(active_chaos.values())):
        try:
            _delete_chaos(command, manifest)
        except Exception as exc:  # pragma: no cover - exceptional kubectl cleanup
            cleanup_errors.append(exc)
    for journey in reversed(tuple(active_journeys.values())):
        try:
            _delete_owned(journey.job_name, runner=command)
        except Exception as exc:  # pragma: no cover - exceptional kubectl cleanup
            cleanup_errors.append(exc)
    if flags is not None:
        try:
            flags.restore()
        except Exception as exc:  # pragma: no cover - exceptional kubectl cleanup
            cleanup_errors.append(exc)

    if primary_error is not None:
        for cleanup_error in cleanup_errors:
            primary_error.add_note(f"cleanup also failed: {cleanup_error}")
        raise primary_error.with_traceback(primary_error.__traceback__)
    if cleanup_errors:
        raise ExceptionGroup("stimulus cleanup failed", cleanup_errors)
    return tuple(sorted(completed, key=lambda item: item.stimulus_id))


class _FlagdController:
    def __init__(self, runner: CommandRunner, stimuli: tuple[FlagdStimulus, ...]) -> None:
        self._runner = runner
        response = runner(
            [
                "kubectl",
                "--context",
                _CONTEXT,
                "-n",
                _NAMESPACE,
                "get",
                "configmap",
                _FLAG_CONFIG_MAP,
                "-o",
                "json",
            ]
        )
        outer = _json_object(response, "flagd ConfigMap")
        data = outer.get("data")
        if not isinstance(data, dict) or not isinstance(data.get(_FLAG_DOCUMENT_KEY), str):
            raise ValueError("flagd ConfigMap lacks demo.flagd.json")
        self._original_text = cast(str, data[_FLAG_DOCUMENT_KEY])
        self._original = _json_object(self._original_text, "flagd document")
        flags = self._original.get("flags")
        if not isinstance(flags, dict):
            raise ValueError("flagd document lacks flags")
        for stimulus in stimuli:
            definition = flags.get(stimulus.flag)
            if not isinstance(definition, dict):
                raise ValueError(f"unknown flagd flag: {stimulus.flag}")
            variants = definition.get("variants")
            if not isinstance(variants, dict) or stimulus.variant not in variants:
                raise ValueError(f"unknown flagd variant: {stimulus.flag}={stimulus.variant}")
            if definition.get("defaultVariant") == stimulus.variant:
                raise ValueError(f"flagd stimulus is a no-op: {stimulus.flag}")
        self._active: dict[str, str] = {}
        self._dirty = False

    def activate(self, stimulus: FlagdStimulus) -> None:
        self._active[stimulus.flag] = stimulus.variant
        self._apply_desired()

    def deactivate(self, stimulus: FlagdStimulus) -> None:
        del self._active[stimulus.flag]
        self._apply_desired()

    def restore(self) -> None:
        if self._dirty:
            self._patch_and_rollout(self._original_text)
            self._dirty = False

    def _apply_desired(self) -> None:
        if not self._active:
            desired = self._original_text
        else:
            document = deepcopy(self._original)
            flags = cast(dict[str, Any], document["flags"])
            for flag, variant in self._active.items():
                definition = cast(dict[str, Any], flags[flag])
                definition["defaultVariant"] = variant
            desired = json.dumps(document, allow_nan=False, indent=2, sort_keys=True) + "\n"
        self._patch_and_rollout(desired)
        self._dirty = bool(self._active)

    def _patch_and_rollout(self, document: str) -> None:
        patch = json.dumps(
            {"data": {_FLAG_DOCUMENT_KEY: document}},
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        self._runner(
            [
                "kubectl",
                "--context",
                _CONTEXT,
                "-n",
                _NAMESPACE,
                "patch",
                "configmap",
                _FLAG_CONFIG_MAP,
                "--type",
                "merge",
                "-p",
                patch,
            ]
        )
        self._dirty = True
        self._runner(
            [
                "kubectl",
                "--context",
                _CONTEXT,
                "-n",
                _NAMESPACE,
                "rollout",
                "restart",
                f"deployment/{_FLAG_DEPLOYMENT}",
            ]
        )
        self._runner(
            [
                "kubectl",
                "--context",
                _CONTEXT,
                "-n",
                _NAMESPACE,
                "rollout",
                "status",
                f"deployment/{_FLAG_DEPLOYMENT}",
                "--timeout=60s",
            ],
            timeout=75,
        )


def _stimulus_transitions(stimuli: tuple[Stimulus, ...]) -> tuple[_StimulusTransition, ...]:
    transitions = tuple(
        transition
        for stimulus in stimuli
        for transition in (
            _StimulusTransition(stimulus.start_offset_seconds, "start", stimulus),
            _StimulusTransition(
                stimulus.start_offset_seconds + stimulus.duration_seconds,
                "stop",
                stimulus,
            ),
        )
    )
    return tuple(
        sorted(
            transitions,
            key=lambda item: (
                item.offset_seconds,
                _transition_priority(item),
                item.stimulus.stimulus_id,
            ),
        )
    )


def _transition_priority(transition: _StimulusTransition) -> int:
    if transition.operation == "start":
        if isinstance(transition.stimulus, FlagdStimulus):
            return 3
        if isinstance(transition.stimulus, ChaosMeshStimulus):
            return 4
        return 5
    if isinstance(transition.stimulus, K6JourneyStimulus):
        return 0
    if isinstance(transition.stimulus, ChaosMeshStimulus):
        return 1
    return 2


def _stimulus_setting(stimulus: Stimulus) -> str:
    if isinstance(stimulus, FlagdStimulus):
        return stimulus.variant
    if isinstance(stimulus, ChaosMeshStimulus):
        return stimulus.experiment
    return f"{stimulus.journey}@{stimulus.rate_rps}rps"


def _start_checkout_journey(
    runner: CommandRunner,
    *,
    repo_root: Path,
    schedule: CompiledSchedule,
    stimulus: K6JourneyStimulus,
    invocation: str,
) -> _JourneyRun:
    identity = f"{schedule.scenario_id}:{schedule.seed}:{stimulus.stimulus_id}:{invocation}"
    suffix = hashlib.sha256(identity.encode()).hexdigest()[:20]
    job_name = f"stim-{suffix}"
    run_id = f"journey-{suffix}"
    job = build_checkout_journey_job(stimulus, job_name=job_name, run_id=run_id)
    script = repo_root / "lab" / "loadgen" / "scenario.js"
    if not script.is_file():
        raise ValueError("checkout journey script is missing")
    _delete_owned(job_name, runner=runner)
    try:
        runner(
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
        runner(
            ["kubectl", "--context", _CONTEXT, "apply", "-f", "-"],
            input_text=yaml.safe_dump(job, sort_keys=False),
        )
    except BaseException as exc:
        try:
            _delete_owned(job_name, runner=runner)
        except Exception as cleanup_error:  # pragma: no cover - exceptional kubectl cleanup
            exc.add_note(f"checkout journey cleanup also failed: {cleanup_error}")
        raise
    return _JourneyRun(
        job_name=job_name,
        user_agent=f"sentinel-stimulus/{run_id}",
        expected_spans=stimulus.rate_rps * stimulus.duration_seconds * 3,
        timeout_seconds=stimulus.duration_seconds + 90,
    )


def _finish_checkout_journey(
    runner: CommandRunner,
    *,
    journey: _JourneyRun,
    span_reader: Callable[[str, int], tuple[datetime, ...]],
) -> tuple[datetime, ...]:
    _wait_for_job(journey.job_name, timeout_seconds=journey.timeout_seconds, runner=runner)
    log = runner(
        [
            "kubectl",
            "--context",
            _CONTEXT,
            "-n",
            _NAMESPACE,
            "logs",
            f"job/{journey.job_name}",
        ]
    )
    if "iterations" not in log or "dropped_iterations" not in log:
        raise RuntimeError(
            f"checkout journey completion log lacked workload evidence for {journey.job_name}"
        )
    timestamps = span_reader(journey.user_agent, journey.expected_spans)
    if not timestamps:
        raise RuntimeError(f"checkout journey emitted no ingress spans: {journey.job_name}")
    minimum = math.ceil(journey.expected_spans * 0.95)
    if len(timestamps) < minimum:
        raise RuntimeError(
            f"checkout journey telemetry incomplete for {journey.job_name}: "
            f"{len(timestamps)}/{journey.expected_spans} spans"
        )
    _delete_owned(journey.job_name, runner=runner)
    return tuple(sorted(timestamps))


def _load_chaos_manifest(repo_root: Path, stimulus: ChaosMeshStimulus) -> _ChaosManifest:
    path = (repo_root / "lab" / "testbed" / "chaos" / f"{stimulus.experiment}.yaml").resolve()
    owned = (repo_root / "lab" / "testbed" / "chaos").resolve()
    if path.parent != owned or not path.is_file():
        raise ValueError(f"unknown committed Chaos Mesh experiment: {stimulus.experiment}")
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError(f"invalid Chaos Mesh manifest: {path.name}")
    kind = document.get("kind")
    metadata = document.get("metadata")
    spec = document.get("spec")
    if kind not in _ALLOWED_CHAOS_KINDS:
        raise ValueError(f"unsupported Chaos Mesh kind: {kind}")
    if not isinstance(metadata, dict) or not isinstance(spec, dict):
        raise ValueError(f"invalid Chaos Mesh manifest: {path.name}")
    if metadata.get("name") != stimulus.experiment or metadata.get("namespace") != _NAMESPACE:
        raise ValueError(f"Chaos Mesh manifest identity mismatch: {path.name}")
    selector = spec.get("selector")
    if not isinstance(selector, dict) or selector.get("namespaces") != [_NAMESPACE]:
        raise ValueError(f"Chaos Mesh selector must be confined to {_NAMESPACE}: {path.name}")
    duration = spec.get("duration")
    if not isinstance(duration, str) or not (
        0 < _duration_seconds(duration) <= _MAX_CHAOS_DURATION_SECONDS
    ):
        raise ValueError(f"Chaos Mesh duration is missing or unbounded: {path.name}")
    if _contains_key(document, "externalTargets"):
        raise ValueError(f"Chaos Mesh external targets are forbidden: {path.name}")
    return _ChaosManifest(path=path, kind=cast(str, kind), name=stimulus.experiment)


def _apply_chaos(runner: CommandRunner, manifest: _ChaosManifest) -> None:
    runner(["kubectl", "--context", _CONTEXT, "apply", "-f", str(manifest.path)])
    runner(
        [
            "kubectl",
            "--context",
            _CONTEXT,
            "-n",
            _NAMESPACE,
            "wait",
            f"{manifest.kind.lower()}/{manifest.name}",
            "--for=condition=AllInjected",
            "--timeout=60s",
        ],
        timeout=75,
    )


def _delete_chaos(runner: CommandRunner, manifest: _ChaosManifest) -> None:
    runner(
        [
            "kubectl",
            "--context",
            _CONTEXT,
            "-n",
            _NAMESPACE,
            "delete",
            "-f",
            str(manifest.path),
            "--ignore-not-found",
            "--wait=true",
        ]
    )


def _duration_seconds(value: str) -> int:
    match = re.fullmatch(r"(\d+)([smh])", value)
    if match is None:
        return 0
    multiplier = {"s": 1, "m": 60, "h": 3600}[match.group(2)]
    return int(match.group(1)) * multiplier


def _contains_key(value: object, target: str) -> bool:
    if isinstance(value, dict):
        return target in value or any(_contains_key(item, target) for item in value.values())
    if isinstance(value, list):
        return any(_contains_key(item, target) for item in value)
    return False


def _json_object(value: str, label: str) -> dict[str, Any]:
    try:
        document = json.loads(value)
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise ValueError(f"invalid {label}: {exc}") from exc
    if not isinstance(document, dict):
        raise ValueError(f"invalid {label}: root must be an object")
    return cast(dict[str, Any], document)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _require_utc(value: datetime, label: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError(f"{label} must be timezone-aware UTC")
    return value


def _wait_until(target: datetime) -> None:
    while (remaining := (target - _utc_now()).total_seconds()) > 0:
        time.sleep(min(remaining, 1.0))


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


def _wait_for_job(
    name: str,
    *,
    timeout_seconds: int,
    runner: CommandRunner | None = None,
) -> None:
    command = _run if runner is None else runner
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        document = json.loads(
            command(
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
            log = command(
                ["kubectl", "--context", _CONTEXT, "-n", _NAMESPACE, "logs", f"job/{name}"]
            )
            raise RuntimeError(f"k6 job failed: {name}\n{log}")
        time.sleep(2)
    raise RuntimeError(f"k6 job timed out: {name}")


def _wait_for_anchor(*, repo_root: Path, user_agent: str) -> datetime:
    deadline = time.monotonic() + 20
    latest_timestamps: tuple[datetime, ...] = ()
    previous_count = 0
    stable_polls = 0
    while time.monotonic() < deadline:
        timestamps = _query_spans(repo_root=repo_root, user_agent=user_agent)
        if len(timestamps) >= 10:
            return timestamps[-1]
        if timestamps:
            latest_timestamps = timestamps
            if len(timestamps) == previous_count:
                stable_polls += 1
            else:
                stable_polls = 0
            previous_count = len(timestamps)
            if stable_polls >= 3:
                return timestamps[-1]
        time.sleep(2)
    if latest_timestamps:
        return latest_timestamps[-1]
    raise RuntimeError(f"scenario start marker did not arrive for {user_agent}")


def _query_spans(*, repo_root: Path, user_agent: str) -> tuple[datetime, ...]:
    query = """
        SELECT observation_id, toUnixTimestamp64Micro(min(ts)) AS ts_us
        FROM sentinel.observations
        PREWHERE service = 'frontend-proxy'
          AND signal = 'span.duration_ms'
        WHERE JSONExtractInt(attributes_json, 'span.kind') = 2
          AND JSONExtractString(attributes_json, 'user_agent') = {user_agent:String}
        GROUP BY observation_id
        ORDER BY ts_us, observation_id
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


def _delete_owned(name: str, *, runner: CommandRunner | None = None) -> None:
    _require_safe(name, "resource name")
    command = _run if runner is None else runner
    command(
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
    command(
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
