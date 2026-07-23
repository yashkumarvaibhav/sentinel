"""Versioned decomposition score floors and fail-closed evaluation."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from yaml.constructor import ConstructorError
from yaml.nodes import MappingNode

from contracts import SymptomKind
from lab.scoring.evaluator import (
    SCORED_SYMPTOM_KINDS,
    EpisodeRunScore,
    RunScore,
    SymptomKindScore,
)
from lab.scoring.metrics import BinaryMetrics, MetricValue, binary_metrics, episode_metrics

type Probability = Annotated[float, Field(ge=0.0, le=1.0, allow_inf_nan=False)]


class ScoreGateConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, allow_inf_nan=False)

    version: Literal[1]
    residual_precision_min: Probability
    residual_recall_min: Probability
    quiet_day_false_positive_rate_max: Probability
    telemetry_completeness_min: Probability
    symptom_precision_min: dict[str, Probability]
    symptom_recall_min: dict[str, Probability]

    @model_validator(mode="after")
    def complete_symptom_floors(self) -> ScoreGateConfig:
        expected = {kind.value for kind in SCORED_SYMPTOM_KINDS}
        for field_name in ("symptom_precision_min", "symptom_recall_min"):
            actual = set(getattr(self, field_name))
            if actual != expected:
                raise ValueError(
                    f"{field_name} must exactly cover scored symptom kinds: "
                    f"missing={sorted(expected - actual)}, extra={sorted(actual - expected)}"
                )
        return self


@dataclass(frozen=True)
class GateFailure:
    metric: str
    actual: float | None
    requirement: str
    scope: str


@dataclass(frozen=True)
class GateResult:
    passed: bool
    overall: BinaryMetrics
    quiet_day: BinaryMetrics
    failures: tuple[GateFailure, ...]


@dataclass(frozen=True)
class SymptomGateResult:
    passed: bool
    by_kind: tuple[SymptomKindScore, ...]
    required_kinds: tuple[SymptomKind, ...]
    failures: tuple[GateFailure, ...]


def load_gate_config(path: Path) -> ScoreGateConfig:
    try:
        document = yaml.load(path.read_text(encoding="utf-8"), Loader=_UniqueKeyLoader)
        return ScoreGateConfig.model_validate(document)
    except (OSError, UnicodeError, yaml.YAMLError, ValidationError) as exc:
        raise ValueError(f"invalid score gate config {path.name}: {exc}") from exc


def evaluate_gates(runs: tuple[RunScore, ...], config: ScoreGateConfig) -> GateResult:
    predicted = tuple(item for run in runs for item in run.predicted)
    expected = tuple(item for run in runs for item in run.expected)
    quiet_runs = tuple(run for run in runs if run.scenario_id == "quiet_day")
    quiet_predicted = tuple(item for run in quiet_runs for item in run.predicted)
    quiet_expected = tuple(item for run in quiet_runs for item in run.expected)
    overall = binary_metrics(predicted=predicted, expected=expected)
    quiet = binary_metrics(predicted=quiet_predicted, expected=quiet_expected)
    failures: list[GateFailure] = []
    _minimum(
        failures,
        "residual_precision",
        overall.precision,
        config.residual_precision_min,
        "overall",
    )
    _minimum(
        failures,
        "residual_recall",
        overall.recall,
        config.residual_recall_min,
        "overall",
    )
    _maximum(
        failures,
        "quiet_day_false_positive_rate",
        quiet.false_positive_rate,
        config.quiet_day_false_positive_rate_max,
        "quiet_day",
    )
    for run in runs:
        if run.telemetry_completeness < config.telemetry_completeness_min:
            failures.append(
                GateFailure(
                    metric="telemetry_completeness",
                    actual=run.telemetry_completeness,
                    requirement=f">={config.telemetry_completeness_min:.3f}",
                    scope=f"{run.scenario_id}/seed-{run.seed}",
                )
            )
    if not runs:
        failures.append(
            GateFailure(
                metric="telemetry_completeness",
                actual=None,
                requirement="at least one held-out run",
                scope="overall",
            )
        )
    return GateResult(
        passed=not failures,
        overall=overall,
        quiet_day=quiet,
        failures=tuple(failures),
    )


def evaluate_symptom_gates(
    runs: tuple[EpisodeRunScore, ...],
    config: ScoreGateConfig,
    *,
    required_kinds: tuple[SymptomKind, ...] = SCORED_SYMPTOM_KINDS,
) -> SymptomGateResult:
    """Gate each required kind on recall/coverage; record precision, do not gate it.

    Phase 2's job is to *detect every real symptom* (recall). A single injected
    fault legitimately produces a topological storm of propagated and secondary
    episodes, so gating exact precision here would pressure the detectors to emit
    fewer real symptoms, fighting "decompose, don't threshold". Collapsing that
    storm into one incident with one root cause -- i.e. the precision/FP
    accounting -- is Phase 4 causal-collapse's job. Precision stays in the score
    and report for transparency but is not a Phase-2 gate.
    """
    if not required_kinds or len(required_kinds) != len(set(required_kinds)):
        raise ValueError("required symptom kinds must be non-empty and unique")
    unsupported = tuple(kind for kind in required_kinds if kind not in SCORED_SYMPTOM_KINDS)
    if unsupported:
        raise ValueError(f"unsupported scored symptom kind: {unsupported[0].value}")

    by_kind = tuple(_aggregate_kind(runs, kind) for kind in SCORED_SYMPTOM_KINDS)
    failures: list[GateFailure] = []
    for kind in required_kinds:
        score = next(item for item in by_kind if item.kind is kind)
        _minimum(
            failures,
            "symptom_recall",
            score.metrics.recall,
            config.symptom_recall_min[kind.value],
            kind.value,
        )
    return SymptomGateResult(
        passed=not failures,
        by_kind=by_kind,
        required_kinds=required_kinds,
        failures=tuple(failures),
    )


def _aggregate_kind(
    runs: tuple[EpisodeRunScore, ...],
    kind: SymptomKind,
) -> SymptomKindScore:
    scores = tuple(run.score_for(kind) for run in runs)
    predicted_count = sum(item.predicted_count for item in scores)
    expected_count = sum(item.expected_count for item in scores)
    matches = tuple(match for score in scores for match in score.matches)
    return SymptomKindScore(
        kind=kind,
        predicted_count=predicted_count,
        expected_count=expected_count,
        metrics=episode_metrics(
            matched_count=len(matches),
            predicted_count=predicted_count,
            expected_count=expected_count,
        ),
        matches=matches,
    )


def _minimum(
    failures: list[GateFailure],
    name: str,
    metric: MetricValue,
    floor: float,
    scope: str,
) -> None:
    if metric.value is None or metric.value < floor:
        failures.append(
            GateFailure(
                metric=name,
                actual=metric.value,
                requirement=f">={floor:.3f}",
                scope=scope,
            )
        )


def _maximum(
    failures: list[GateFailure],
    name: str,
    metric: MetricValue,
    ceiling: float,
    scope: str,
) -> None:
    if metric.value is None or metric.value > ceiling:
        failures.append(
            GateFailure(
                metric=name,
                actual=metric.value,
                requirement=f"<={ceiling:.3f}",
                scope=scope,
            )
        )


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
