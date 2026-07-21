"""Deterministic Drain3 template mining and log-frequency burst symptoms."""

from __future__ import annotations

import hashlib
import json
import math
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import cast

from drain3.drain import LogCluster
from drain3.template_miner import TemplateMiner
from drain3.template_miner_config import TemplateMinerConfig

from common.config import LogTemplateConfig
from contracts import Symptom, SymptomKind


@dataclass(frozen=True, slots=True)
class LogLine:
    """Minimal label-free log evidence accepted by the detector."""

    log_id: str
    ts: datetime
    service: str
    message: str

    def __post_init__(self) -> None:
        for name in ("log_id", "service", "message"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")
        if len(self.message) > 2_000:
            raise ValueError("message must contain at most 2000 characters")
        if not isinstance(self.ts, datetime) or self.ts.utcoffset() != timedelta(0):
            raise ValueError("ts must be timezone-aware UTC")
        object.__setattr__(self, "log_id", self.log_id.strip())
        object.__setattr__(self, "service", self.service.strip())
        object.__setattr__(self, "message", self.message.strip())
        object.__setattr__(self, "ts", self.ts.astimezone(UTC))


@dataclass(frozen=True, slots=True)
class TemplateFrequency:
    """One mined template's complete frequency evidence for a fixed window."""

    template_id: str
    service: str
    template: str
    count: int
    messages_per_second: float
    first_ts: datetime
    evidence_refs: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class LogDetectionResult:
    frequencies: tuple[TemplateFrequency, ...]
    symptoms: tuple[Symptom, ...]


class LogTemplateBurstDetector:
    """Maintain service-local templates and compare window rates with learned baselines."""

    def __init__(self, *, configuration: LogTemplateConfig) -> None:
        self._configuration = configuration
        self._miners: dict[str, TemplateMiner] = {}

    def mine_window(
        self,
        records: Sequence[LogLine],
        *,
        window_seconds: float,
    ) -> tuple[TemplateFrequency, ...]:
        """Mine event-time-ordered logs and return final-template window frequencies."""
        duration = _validate_window_seconds(window_seconds)
        ordered = tuple(sorted(records, key=lambda record: (record.ts, record.log_id)))
        log_ids = [record.log_id for record in ordered]
        if len(log_ids) != len(set(log_ids)):
            raise ValueError("log_id values must be unique within a window")

        for record in ordered:
            miner = self._miners.get(record.service)
            if miner is None:
                miner = self._new_miner()
                self._miners[record.service] = miner
            miner.add_log_message(record.message)

        grouped: dict[tuple[str, int], list[LogLine]] = defaultdict(list)
        clusters: dict[tuple[str, int], LogCluster] = {}
        for record in ordered:
            cluster = cast(
                LogCluster | None,
                self._miners[record.service].match(
                    record.message,
                    full_search_strategy="always",
                ),
            )
            if cluster is None:
                raise RuntimeError("Drain3 could not rematch a log it just mined")
            key = (record.service, cluster.cluster_id)
            grouped[key].append(record)
            clusters[key] = cluster

        frequencies: list[TemplateFrequency] = []
        for key in sorted(grouped):
            service, cluster_id = key
            evidence = grouped[key]
            template = clusters[key].get_template()  # type: ignore[no-untyped-call]
            template_id = _digest({"service": service, "drain_cluster_id": cluster_id})
            frequencies.append(
                TemplateFrequency(
                    template_id=template_id,
                    service=service,
                    template=template,
                    count=len(evidence),
                    messages_per_second=len(evidence) / duration,
                    first_ts=evidence[0].ts,
                    evidence_refs=tuple(record.log_id for record in evidence),
                )
            )
        return tuple(frequencies)

    def detect_window(
        self,
        records: Sequence[LogLine],
        *,
        window_seconds: float,
        baseline_rates: Mapping[str, float],
    ) -> LogDetectionResult:
        """Mine one window and emit configured upward template-frequency deformations."""
        baselines = _validate_baselines(baseline_rates)
        frequencies = self.mine_window(records, window_seconds=window_seconds)
        symptoms: list[Symptom] = []
        for frequency in frequencies:
            if frequency.count < self._configuration.minimum_template_count:
                continue
            baseline = baselines.get(frequency.template_id, 0.0)
            deformation = max(frequency.messages_per_second - baseline, 0.0) / max(
                baseline,
                self._configuration.baseline_rate_floor,
            )
            if deformation < self._configuration.trigger_relative_deformation:
                continue
            score = min(deformation / self._configuration.full_score_relative_deformation, 1.0)
            note = (
                f"log template frequency rose beyond baseline: template_id="
                f"{frequency.template_id}, template={frequency.template!r}, "
                f"current_per_second={_render(frequency.messages_per_second)}, "
                f"baseline_per_second={_render(baseline)}, count={frequency.count}, "
                f"relative_deformation={_render(deformation)}, "
                f"trigger={_render(self._configuration.trigger_relative_deformation)}."
            )
            identity = {
                "template_id": frequency.template_id,
                "service": frequency.service,
                "template": frequency.template,
                "first_ts": frequency.first_ts.isoformat(),
                "count": frequency.count,
                "messages_per_second": frequency.messages_per_second,
                "baseline": baseline,
                "relative_deformation": deformation,
                "evidence_refs": frequency.evidence_refs,
            }
            symptoms.append(
                Symptom(
                    symptom_id=_digest(identity),
                    kind=SymptomKind.LOG_BURST,
                    service=frequency.service,
                    signal="log_template_rate",
                    onset_ts=frequency.first_ts,
                    score=score,
                    note=note,
                    evidence_refs=frequency.evidence_refs,
                )
            )
        return LogDetectionResult(frequencies=frequencies, symptoms=tuple(symptoms))

    def _new_miner(self) -> TemplateMiner:
        config = TemplateMinerConfig()  # type: ignore[no-untyped-call]
        config.profiling_enabled = False
        config.drain_sim_th = self._configuration.similarity_threshold
        config.drain_depth = self._configuration.max_depth
        config.drain_max_children = self._configuration.max_children
        config.drain_max_clusters = self._configuration.max_clusters
        config.parametrize_numeric_tokens = self._configuration.parameterize_numeric_tokens
        return TemplateMiner(config=config)


def _validate_window_seconds(value: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError("window_seconds must be a number")
    duration = float(value)
    if not math.isfinite(duration) or duration <= 0.0:
        raise ValueError("window_seconds must be finite and greater than zero")
    return duration


def _validate_baselines(values: Mapping[str, float]) -> dict[str, float]:
    if not isinstance(values, Mapping):
        raise TypeError("baseline_rates must be a mapping")
    baselines: dict[str, float] = {}
    for template_id, value in values.items():
        if not isinstance(template_id, str) or not template_id.strip():
            raise ValueError("baseline template IDs must be non-empty strings")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise TypeError("baseline rates must be numbers")
        baseline = float(value)
        if not math.isfinite(baseline) or baseline < 0.0:
            raise ValueError("baseline rates must be finite and non-negative")
        baselines[template_id.strip()] = baseline
    return baselines


def _render(value: float) -> str:
    return format(value, ".12g")


def _digest(value: object) -> str:
    encoded = json.dumps(
        value,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()
