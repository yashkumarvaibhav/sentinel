"""Deterministic compilation into public stimulus/context and private labels."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal, cast

import yaml
from pydantic import ValidationError
from yaml.constructor import ConstructorError
from yaml.nodes import MappingNode

from lab.scenarios.capabilities import validate_symptom_label_capabilities
from lab.scenarios.models import ScenarioProfile, Stimulus, SymptomLabelInterval


class SeedPurpose(StrEnum):
    DEVELOPMENT = "development"
    HELD_OUT = "held_out"
    AD_HOC = "ad_hoc"


@dataclass(frozen=True)
class CompiledLoadPhase:
    name: str
    start_offset_seconds: int
    duration_seconds: int
    rate_rps: int


@dataclass(frozen=True)
class CompiledSchedule:
    version: Literal[1]
    scenario_id: str
    honesty: Literal["SIMULATED"]
    seed: int
    seed_purpose: SeedPurpose
    request_mix_seed: int
    target: Literal["astronomy-shop/frontend-proxy"]
    phases: tuple[CompiledLoadPhase, ...]
    stimuli: tuple[Stimulus, ...]


@dataclass(frozen=True)
class ScenarioArtifacts:
    schedule: CompiledSchedule
    context_feed: dict[str, object]
    labels: dict[str, object]

    def canonical_bytes(self) -> bytes:
        return _json_bytes(
            {
                "schedule": _schedule_payload(self.schedule),
                "context_feed": self.context_feed,
                "labels": self.labels,
            }
        )


@dataclass(frozen=True)
class ArtifactPaths:
    schedule: Path
    context_feed: Path
    labels: Path


class ScenarioLoadError(ValueError):
    """A committed scenario file is malformed or ambiguous."""


def load_profile(path: Path) -> ScenarioProfile:
    try:
        document = yaml.load(path.read_text(encoding="utf-8"), Loader=_UniqueKeyLoader)
        if not isinstance(document, dict):
            raise ScenarioLoadError("scenario root must be a mapping")
        return ScenarioProfile.model_validate(document)
    except (OSError, UnicodeError, yaml.YAMLError, ValidationError) as exc:
        raise ScenarioLoadError(f"{path.name}: {exc}") from exc


def compile_profile(
    profile: ScenarioProfile,
    *,
    seed: int,
    purpose: SeedPurpose,
) -> ScenarioArtifacts:
    validate_symptom_label_capabilities(profile)
    allowed = {
        SeedPurpose.DEVELOPMENT: profile.seeds.development,
        SeedPurpose.HELD_OUT: profile.seeds.held_out,
    }
    if purpose in allowed and seed not in allowed[purpose]:
        raise ValueError(f"seed {seed} is not in the {purpose.value} set")
    offset = 0
    phases: list[CompiledLoadPhase] = []
    for phase in profile.load_phases:
        phases.append(
            CompiledLoadPhase(
                name=phase.name,
                start_offset_seconds=offset,
                duration_seconds=phase.duration_seconds,
                rate_rps=phase.rate_rps,
            )
        )
        offset += phase.duration_seconds
    request_mix_seed = int.from_bytes(
        hashlib.sha256(f"{profile.scenario_id}:{seed}".encode()).digest()[:8], "big"
    )
    schedule = CompiledSchedule(
        version=1,
        scenario_id=profile.scenario_id,
        honesty=profile.honesty,
        seed=seed,
        seed_purpose=purpose,
        request_mix_seed=request_mix_seed,
        target=profile.telemetry.target,
        phases=tuple(phases),
        stimuli=profile.stimuli,
    )
    context_feed: dict[str, object] = {
        "version": 1,
        "scenario_id": profile.scenario_id,
        "honesty": profile.honesty,
        "windows": [context.model_dump(mode="json") for context in profile.contexts],
    }
    labels: dict[str, object] = {
        "version": 1,
        "scenario_id": profile.scenario_id,
        "seed": seed,
        "seed_purpose": purpose.value,
        "intervals": [
            label.model_dump(mode="json", exclude_none=True) for label in profile.residual_labels
        ],
        "symptom_intervals": [
            label.model_dump(mode="json", exclude_none=True) for label in profile.symptom_labels
        ],
    }
    return ScenarioArtifacts(schedule=schedule, context_feed=context_feed, labels=labels)


def write_artifacts(artifacts: ScenarioArtifacts, output: Path) -> ArtifactPaths:
    private = output / "private"
    private.mkdir(parents=True, exist_ok=True)
    schedule_path = output / "schedule.json"
    context_path = output / "context-feed.json"
    labels_path = private / "labels.json"
    schedule_path.write_bytes(_json_bytes(_schedule_payload(artifacts.schedule)))
    context_path.write_bytes(_json_bytes(artifacts.context_feed))
    labels_path.write_bytes(_json_bytes(artifacts.labels))
    return ArtifactPaths(schedule=schedule_path, context_feed=context_path, labels=labels_path)


def schedule_payload(schedule: CompiledSchedule) -> dict[str, object]:
    """Return the public k6 input without any private labels."""
    return _schedule_payload(schedule)


def materialize_symptom_labels(
    artifacts: ScenarioArtifacts,
    *,
    stimulus_offsets: Mapping[str, tuple[float, float]],
) -> dict[str, object]:
    """Shift every stimulus-backed private label to measured execution offsets."""
    raw_residual = artifacts.labels.get("intervals")
    if not isinstance(raw_residual, list):
        raise ValueError("compiled labels lack intervals")
    residual_intervals: list[dict[str, object]] = []
    for raw_interval in raw_residual:
        if not isinstance(raw_interval, dict):
            raise ValueError("compiled residual interval is not an object")
        payload = cast(dict[str, object], dict(raw_interval))
        _apply_measured_offset(payload, stimulus_offsets=stimulus_offsets)
        residual_intervals.append(payload)

    raw_intervals = artifacts.labels.get("symptom_intervals")
    if not isinstance(raw_intervals, list):
        raise ValueError("compiled labels lack symptom_intervals")
    intervals: list[dict[str, object]] = []
    for raw_interval in raw_intervals:
        if not isinstance(raw_interval, dict):
            raise ValueError("compiled symptom interval is not an object")
        payload = cast(dict[str, object], dict(raw_interval))
        _apply_measured_offset(payload, stimulus_offsets=stimulus_offsets)
        intervals.append(
            SymptomLabelInterval.model_validate(payload).model_dump(mode="json", exclude_none=True)
        )
    labels = dict(artifacts.labels)
    labels["intervals"] = residual_intervals
    labels["symptom_intervals"] = intervals
    return labels


def _apply_measured_offset(
    payload: dict[str, object],
    *,
    stimulus_offsets: Mapping[str, tuple[float, float]],
) -> None:
    stimulus_id = payload.get("stimulus_id")
    if stimulus_id is None:
        return
    if not isinstance(stimulus_id, str) or stimulus_id not in stimulus_offsets:
        raise ValueError(f"missing measured execution for stimulus: {stimulus_id}")
    start, end = stimulus_offsets[stimulus_id]
    payload["start_offset_seconds"] = start
    payload["end_offset_seconds"] = end


def _schedule_payload(schedule: CompiledSchedule) -> dict[str, object]:
    return {
        "version": schedule.version,
        "scenario_id": schedule.scenario_id,
        "honesty": schedule.honesty,
        "seed": schedule.seed,
        "seed_purpose": schedule.seed_purpose.value,
        "request_mix_seed": schedule.request_mix_seed,
        "target": schedule.target,
        "phases": [
            {
                "name": phase.name,
                "start_offset_seconds": phase.start_offset_seconds,
                "duration_seconds": phase.duration_seconds,
                "rate_rps": phase.rate_rps,
            }
            for phase in schedule.phases
        ],
        "stimuli": [stimulus.model_dump(mode="json") for stimulus in schedule.stimuli],
    }


def _json_bytes(value: object) -> bytes:
    return (
        json.dumps(value, allow_nan=False, separators=(",", ":"), sort_keys=True) + "\n"
    ).encode()


class _UniqueKeyLoader(yaml.SafeLoader):
    pass


def _construct_mapping(loader: _UniqueKeyLoader, node: MappingNode, deep: bool = False) -> Any:
    loader.flatten_mapping(node)
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                f"duplicate key: {key!r}",
                key_node.start_mark,
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_mapping,
)
