"""Behavioral ratios stay scale-free and emit only evidenced deformations."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest
from hypothesis import given
from hypothesis import strategies as st

from contracts import SymptomKind
from detection.ratios import BehavioralRatio, BehavioralRatioMonitor, shannon_entropy
from tests.factories import behavioral_ratio_config

_TS = datetime(2026, 7, 21, 14, 0, tzinfo=UTC)


@given(
    counts=st.lists(st.integers(min_value=0, max_value=10_000), min_size=1, max_size=32).filter(
        any
    ),
    scale=st.integers(min_value=1, max_value=10_000),
)
def test_shannon_entropy_is_invariant_when_every_source_count_scales(
    counts: list[int], scale: int
) -> None:
    original = {f"source-{index}": count for index, count in enumerate(counts)}
    scaled = {source: count * scale for source, count in original.items()}

    assert shannon_entropy(scaled) == pytest.approx(shannon_entropy(original), abs=1e-12)


def test_all_count_ratios_and_scores_are_invariant_under_volume_scaling() -> None:
    monitor = _monitor()

    auth = monitor.evaluate_auth_failure(
        service="frontend",
        onset_ts=_TS,
        failures=25,
        attempts=100,
        baseline_ratio=0.05,
        evidence_refs=("auth-window",),
    )
    scaled_auth = monitor.evaluate_auth_failure(
        service="frontend",
        onset_ts=_TS,
        failures=25_000,
        attempts=100_000,
        baseline_ratio=0.05,
        evidence_refs=("auth-window",),
    )
    syn_ack = monitor.evaluate_syn_ack(
        service="frontend",
        onset_ts=_TS,
        syn_count=200,
        ack_count=100,
        baseline_ratio=1.0,
        evidence_refs=("flow-window",),
    )
    scaled_syn_ack = monitor.evaluate_syn_ack(
        service="frontend",
        onset_ts=_TS,
        syn_count=20_000,
        ack_count=10_000,
        baseline_ratio=1.0,
        evidence_refs=("flow-window",),
    )
    rpc = monitor.evaluate_rpc_amplification(
        service="checkout",
        onset_ts=_TS,
        retries=60,
        requests=100,
        baseline_ratio=0.1,
        evidence_refs=("trace-window",),
    )
    scaled_rpc = monitor.evaluate_rpc_amplification(
        service="checkout",
        onset_ts=_TS,
        retries=60_000,
        requests=100_000,
        baseline_ratio=0.1,
        evidence_refs=("trace-window",),
    )

    for original, scaled in (
        (auth, scaled_auth),
        (syn_ack, scaled_syn_ack),
        (rpc, scaled_rpc),
    ):
        assert original.current == scaled.current
        assert original.relative_deformation == scaled.relative_deformation
        assert original.symptom is not None
        assert scaled.symptom is not None
        assert original.symptom.score == scaled.symptom.score


def test_fan_volume_preserves_entropy_while_concentrated_flood_deforms_it() -> None:
    monitor = _monitor()
    baseline_counts = {f"fan-{index}": 10 for index in range(16)}
    baseline = shannon_entropy(baseline_counts)
    assert baseline is not None

    fan_surge = monitor.evaluate_source_entropy(
        service="frontend",
        onset_ts=_TS,
        source_counts={source: count * 50 for source, count in baseline_counts.items()},
        baseline_entropy=baseline,
        evidence_refs=("fan-flow-window",),
    )
    concentrated_flood = monitor.evaluate_source_entropy(
        service="frontend",
        onset_ts=_TS,
        source_counts={"bot-controller": 7_985, **{f"fan-{index}": 1 for index in range(15)}},
        baseline_entropy=baseline,
        evidence_refs=("flood-flow-window",),
    )

    assert fan_surge.current == pytest.approx(baseline)
    assert fan_surge.relative_deformation == pytest.approx(0.0)
    assert fan_surge.symptom is None
    assert concentrated_flood.current is not None
    assert concentrated_flood.current < baseline
    assert concentrated_flood.symptom is not None
    assert concentrated_flood.symptom.kind is SymptomKind.RATIO_DEFORM
    assert concentrated_flood.symptom.signal == "source_entropy"
    assert "active_sources=16" in concentrated_flood.symptom.note
    assert "total_samples=8000" in concentrated_flood.symptom.note


def test_auth_failure_deformation_names_values_and_links_raw_evidence() -> None:
    result = _monitor().evaluate_auth_failure(
        service="frontend",
        onset_ts=_TS,
        failures=30,
        attempts=100,
        baseline_ratio=0.05,
        evidence_refs=("window-z", "window-a"),
    )

    assert result.metric is BehavioralRatio.AUTH_FAILURE
    assert result.current == 0.3
    assert result.relative_deformation == 5.0
    assert result.symptom is not None
    assert result.symptom.kind is SymptomKind.RATIO_DEFORM
    assert result.symptom.score == 1.0
    assert result.symptom.evidence_refs == ("window-a", "window-z")
    assert "current=0.3" in result.symptom.note
    assert "baseline=0.05" in result.symptom.note
    assert "failures=30, attempts=100" in result.symptom.note


def test_empty_windows_are_insufficient_evidence_and_never_invent_a_ratio() -> None:
    monitor = _monitor()

    entropy = monitor.evaluate_source_entropy(
        service="frontend",
        onset_ts=_TS,
        source_counts={},
        baseline_entropy=3.0,
        evidence_refs=("empty-flow-window",),
    )
    auth = monitor.evaluate_auth_failure(
        service="frontend",
        onset_ts=_TS,
        failures=0,
        attempts=0,
        baseline_ratio=0.05,
        evidence_refs=("empty-auth-window",),
    )
    syn_ack = monitor.evaluate_syn_ack(
        service="frontend",
        onset_ts=_TS,
        syn_count=0,
        ack_count=0,
        baseline_ratio=1.0,
        evidence_refs=("empty-flow-window",),
    )
    rpc = monitor.evaluate_rpc_amplification(
        service="checkout",
        onset_ts=_TS,
        retries=0,
        requests=0,
        baseline_ratio=0.1,
        evidence_refs=("empty-trace-window",),
    )

    for result in (entropy, auth, syn_ack, rpc):
        assert result.current is None
        assert result.relative_deformation is None
        assert result.symptom is None


def test_positive_numerator_with_zero_denominator_uses_a_visible_configured_ceiling() -> None:
    monitor = _monitor()

    syn_ack = monitor.evaluate_syn_ack(
        service="frontend",
        onset_ts=_TS,
        syn_count=500,
        ack_count=0,
        baseline_ratio=1.0,
        evidence_refs=("syn-window",),
    )
    rpc = monitor.evaluate_rpc_amplification(
        service="checkout",
        onset_ts=_TS,
        retries=12,
        requests=0,
        baseline_ratio=0.1,
        evidence_refs=("rpc-window",),
    )

    assert syn_ack.current == 20.0
    assert rpc.current == 20.0
    assert syn_ack.symptom is not None
    assert rpc.symptom is not None
    assert "ack_count=0, capped_at=20" in syn_ack.symptom.note
    assert "requests=0, capped_at=20" in rpc.symptom.note


def test_symptom_identity_does_not_depend_on_evidence_reference_order() -> None:
    monitor = _monitor()

    first = monitor.evaluate_auth_failure(
        service="frontend",
        onset_ts=_TS,
        failures=30,
        attempts=100,
        baseline_ratio=0.05,
        evidence_refs=("window-b", "window-a"),
    )
    second = monitor.evaluate_auth_failure(
        service="frontend",
        onset_ts=_TS,
        failures=30,
        attempts=100,
        baseline_ratio=0.05,
        evidence_refs=("window-a", "window-b"),
    )

    assert first.symptom == second.symptom


@pytest.mark.parametrize(
    ("call", "message"),
    [
        (
            lambda monitor: monitor.evaluate_auth_failure(
                service="frontend",
                onset_ts=_TS,
                failures=2,
                attempts=1,
                baseline_ratio=0.05,
                evidence_refs=("auth-window",),
            ),
            "failures cannot exceed attempts",
        ),
        (
            lambda monitor: monitor.evaluate_syn_ack(
                service="frontend",
                onset_ts=_TS,
                syn_count=-1,
                ack_count=1,
                baseline_ratio=1.0,
                evidence_refs=("flow-window",),
            ),
            "syn_count must be non-negative",
        ),
        (
            lambda monitor: monitor.evaluate_rpc_amplification(
                service="checkout",
                onset_ts=_TS,
                retries=1,
                requests=10,
                baseline_ratio=21.0,
                evidence_refs=("rpc-window",),
            ),
            "baseline cannot exceed",
        ),
        (
            lambda monitor: monitor.evaluate_source_entropy(
                service="frontend",
                onset_ts=datetime(2026, 7, 21, 15, 0, tzinfo=timezone(timedelta(hours=1))),
                source_counts={"source": 1},
                baseline_entropy=1.0,
                evidence_refs=("flow-window",),
            ),
            "timezone-aware UTC",
        ),
        (
            lambda monitor: monitor.evaluate_auth_failure(
                service="frontend",
                onset_ts=_TS,
                failures=1,
                attempts=10,
                baseline_ratio=0.05,
                evidence_refs=("same", "same"),
            ),
            "must be unique",
        ),
    ],
)
def test_ambiguous_or_invalid_ratio_evidence_fails_closed(call: object, message: str) -> None:
    with pytest.raises((TypeError, ValueError), match=message):
        call(_monitor())  # type: ignore[operator]


def _monitor() -> BehavioralRatioMonitor:
    return BehavioralRatioMonitor(configuration=behavioral_ratio_config())
