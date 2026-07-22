"""Evidence capabilities that constrain private scenario labels."""

from __future__ import annotations

from dataclasses import dataclass

from contracts import SymptomKind
from lab.scenarios.models import (
    ChaosMeshStimulus,
    FlagdStimulus,
    K6JourneyStimulus,
    K6PathAttackStimulus,
    K6RatePhaseStimulus,
    ScenarioProfile,
    Stimulus,
    SymptomLabelInterval,
)


@dataclass(frozen=True, slots=True)
class SymptomCapability:
    """One exact stimulus effect the public detector surface can verify."""

    kind: SymptomKind
    service: str
    signal: str
    evidence: str
    requires_checkout_journey: bool = False


_PAYMENT_FAILURE_CAPABILITIES = (
    SymptomCapability(
        kind=SymptomKind.EDGE_DEGRADED,
        service="checkout",
        signal="dependency.payment",
        evidence="trace-correlated checkout to payment calls",
        requires_checkout_journey=True,
    ),
    SymptomCapability(
        kind=SymptomKind.LOG_BURST,
        service="payment",
        signal="log_template_rate",
        evidence="service-local payment log records",
        requires_checkout_journey=True,
    ),
)

_PATH_ATTACK_CAPABILITIES = (
    SymptomCapability(
        kind=SymptomKind.RATIO_DEFORM,
        service="frontend",
        signal="path_entropy",
        evidence="frontend server spans with measured request paths and timestamps",
    ),
)

_EMAIL_MEMORY_LEAK_CAPABILITIES = (
    SymptomCapability(
        kind=SymptomKind.SATURATION,
        service="email",
        signal="container_memory",
        evidence="email container working-set and Kubernetes hard-limit gauges",
        requires_checkout_journey=True,
    ),
)

_RATE_DROP_CAPABILITIES = (
    SymptomCapability(
        kind=SymptomKind.DROP,
        service="frontend",
        signal="request_rate",
        evidence="low-but-present fixed-target primary ingress spans under event expectation",
    ),
)

_FRONTEND_FAILURE_CAPABILITIES = (
    SymptomCapability(
        kind=SymptomKind.SILENCE,
        service="frontend",
        signal="request_rate",
        evidence="continuing fixed-target workload with absent frontend-proxy ingress spans",
    ),
)

_POSITIVE_KINDS = (
    SymptomKind.RATIO_DEFORM,
    SymptomKind.LOG_BURST,
    SymptomKind.EDGE_DEGRADED,
    SymptomKind.SATURATION,
    SymptomKind.DROP,
    SymptomKind.SILENCE,
)


def capabilities_for(stimulus: Stimulus) -> tuple[SymptomCapability, ...]:
    """Return only effects independently supported by current public evidence."""
    if (
        isinstance(stimulus, FlagdStimulus)
        and stimulus.flag == "paymentFailure"
        and stimulus.variant == "100%"
    ):
        return _PAYMENT_FAILURE_CAPABILITIES
    if isinstance(stimulus, K6PathAttackStimulus):
        return _PATH_ATTACK_CAPABILITIES
    if (
        isinstance(stimulus, FlagdStimulus)
        and stimulus.flag == "emailMemoryLeak"
        and stimulus.variant == "100x"
    ):
        return _EMAIL_MEMORY_LEAK_CAPABILITIES
    if isinstance(stimulus, K6RatePhaseStimulus):
        return _RATE_DROP_CAPABILITIES
    if (
        isinstance(stimulus, ChaosMeshStimulus)
        and stimulus.experiment == "frontend-proxy-pod-failure"
    ):
        return _FRONTEND_FAILURE_CAPABILITIES
    return ()


def validate_symptom_label_capabilities(profile: ScenarioProfile) -> None:
    """Reject answer keys not justified by one exact measured stimulus."""
    by_id = {stimulus.stimulus_id: stimulus for stimulus in profile.stimuli}
    for label in profile.symptom_labels:
        if label.stimulus_id is None:
            raise ValueError(f"symptom label lacks measured stimulus: {label.label_id}")
        stimulus = by_id[label.stimulus_id]
        capability = _matching_capability(stimulus, label)
        if capability is None:
            raise ValueError(
                f"stimulus {label.stimulus_id} does not prove "
                f"{label.kind}/{label.service}/{label.signal}"
            )
        if capability.requires_checkout_journey and not _has_covering_checkout(profile, label):
            raise ValueError(
                f"stimulus {label.stimulus_id} requires an overlapping checkout journey"
            )
        if isinstance(stimulus, K6RatePhaseStimulus) and not _proves_expected_drop(
            profile, label, stimulus
        ):
            raise ValueError(
                f"stimulus {label.stimulus_id} lacks a covering high-volume expectation"
            )
        if (
            isinstance(stimulus, ChaosMeshStimulus)
            and stimulus.experiment == "frontend-proxy-pod-failure"
            and stimulus.duration_seconds < 160
        ):
            raise ValueError(
                f"stimulus {label.stimulus_id} is too short to prove configured silence"
            )

    for residual_label in profile.residual_labels:
        if residual_label.stimulus_id is None:
            continue
        stimulus = by_id[residual_label.stimulus_id]
        if not isinstance(stimulus, K6PathAttackStimulus):
            raise ValueError(
                f"stimulus {residual_label.stimulus_id} does not prove RESIDUAL_EXCEED"
            )


def missing_positive_kinds(profile: ScenarioProfile) -> tuple[SymptomKind, ...]:
    """List kinds that still have no honest positive label in this profile."""
    present = {SymptomKind(label.kind) for label in profile.symptom_labels}
    return tuple(kind for kind in _POSITIVE_KINDS if kind not in present)


def _matching_capability(
    stimulus: Stimulus,
    label: SymptomLabelInterval,
) -> SymptomCapability | None:
    return next(
        (
            capability
            for capability in capabilities_for(stimulus)
            if (
                capability.kind.value,
                capability.service,
                capability.signal,
            )
            == (label.kind, label.service, label.signal)
        ),
        None,
    )


def _has_covering_checkout(
    profile: ScenarioProfile,
    label: SymptomLabelInterval,
) -> bool:
    return any(
        isinstance(stimulus, K6JourneyStimulus)
        and stimulus.journey == "checkout"
        and stimulus.start_offset_seconds <= label.start_offset_seconds
        and stimulus.start_offset_seconds + stimulus.duration_seconds >= label.end_offset_seconds
        for stimulus in profile.stimuli
    )


def _proves_expected_drop(
    profile: ScenarioProfile,
    label: SymptomLabelInterval,
    stimulus: K6RatePhaseStimulus,
) -> bool:
    baseline_rate = profile.load_phases[0].rate_rps
    return any(
        context.start_offset_seconds <= label.start_offset_seconds
        and context.start_offset_seconds + context.duration_seconds >= label.end_offset_seconds
        and (multiplier := context.expected_delta.get("frontend.request_rate")) is not None
        and stimulus.rate_rps * 2 <= baseline_rate * multiplier
        for context in profile.contexts
    )
