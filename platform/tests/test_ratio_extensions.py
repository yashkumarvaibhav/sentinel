"""Path, conversion, timing and crowd ratios distinguish fan and attack behavior."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from hypothesis import given
from hypothesis import strategies as st

from contracts import SymptomKind
from detection.ratios import (
    BehavioralRatio,
    BehavioralRatioMonitor,
    coefficient_of_variation,
    positive_pearson_coherence,
    shannon_entropy,
)
from tests.factories import behavioral_ratio_config

_TS = datetime(2026, 7, 21, 15, 0, tzinfo=UTC)


def test_replayed_fan_window_stays_normal_while_attack_window_deforms_all_features() -> None:
    monitor = _monitor()
    baseline_paths = {"/": 25, "/search": 25, "/product": 25, "/checkout": 25}
    path_baseline = shannon_entropy(baseline_paths)
    fan_gaps = (0.15, 0.8, 1.7, 0.3, 2.1, 0.45, 1.2, 0.6)
    timing_baseline = coefficient_of_variation(fan_gaps)
    assert path_baseline is not None
    assert timing_baseline is not None

    fan_results = (
        monitor.evaluate_path_entropy(
            service="frontend",
            onset_ts=_TS,
            path_counts={path: count * 50 for path, count in baseline_paths.items()},
            baseline_entropy=path_baseline,
            evidence_refs=("fan-http-window",),
        ),
        monitor.evaluate_conversion(
            service="checkout",
            onset_ts=_TS,
            conversions=100,
            visits=1_000,
            baseline_ratio=0.1,
            evidence_refs=("fan-conversion-window",),
        ),
        monitor.evaluate_interarrival_variation(
            service="frontend",
            onset_ts=_TS,
            interarrival_seconds=fan_gaps,
            baseline_cv=timing_baseline,
            evidence_refs=("fan-timing-window",),
        ),
        monitor.evaluate_crowd_coherence(
            service="frontend",
            onset_ts=_TS,
            traffic_values=(10.0, 20.0, 40.0, 80.0, 40.0),
            related_kpi_values=(2.0, 4.0, 8.0, 16.0, 8.0),
            baseline_coherence=0.9,
            evidence_refs=("fan-kpi-window",),
        ),
    )
    attack_results = (
        monitor.evaluate_path_entropy(
            service="frontend",
            onset_ts=_TS,
            path_counts={"/": 9_970, "/search": 10, "/product": 10, "/checkout": 10},
            baseline_entropy=path_baseline,
            evidence_refs=("attack-http-window",),
        ),
        monitor.evaluate_conversion(
            service="checkout",
            onset_ts=_TS,
            conversions=1,
            visits=1_000,
            baseline_ratio=0.1,
            evidence_refs=("attack-conversion-window",),
        ),
        monitor.evaluate_interarrival_variation(
            service="frontend",
            onset_ts=_TS,
            interarrival_seconds=(1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0),
            baseline_cv=timing_baseline,
            evidence_refs=("attack-timing-window",),
        ),
        monitor.evaluate_crowd_coherence(
            service="frontend",
            onset_ts=_TS,
            traffic_values=(1.0, 2.0, 3.0, 4.0, 5.0),
            related_kpi_values=(1.0, 0.0, 1.0, 0.0, 1.0),
            baseline_coherence=0.9,
            evidence_refs=("attack-kpi-window",),
        ),
    )

    assert all(result.symptom is None for result in fan_results)
    assert {result.metric for result in attack_results} == {
        BehavioralRatio.PATH_ENTROPY,
        BehavioralRatio.CONVERSION,
        BehavioralRatio.INTERARRIVAL_VARIATION,
        BehavioralRatio.CROWD_COHERENCE,
    }
    assert all(
        result.symptom is not None and result.symptom.kind is SymptomKind.RATIO_DEFORM
        for result in attack_results
    )


def test_path_and_conversion_ratios_are_volume_scale_invariant() -> None:
    monitor = _monitor()
    baseline = shannon_entropy({"/": 1, "/search": 1, "/checkout": 1})
    assert baseline is not None

    original_path = monitor.evaluate_path_entropy(
        service="frontend",
        onset_ts=_TS,
        path_counts={"/": 98, "/search": 1, "/checkout": 1},
        baseline_entropy=baseline,
        evidence_refs=("path-window",),
    )
    scaled_path = monitor.evaluate_path_entropy(
        service="frontend",
        onset_ts=_TS,
        path_counts={"/": 98_000, "/search": 1_000, "/checkout": 1_000},
        baseline_entropy=baseline,
        evidence_refs=("path-window",),
    )
    original_conversion = monitor.evaluate_conversion(
        service="checkout",
        onset_ts=_TS,
        conversions=2,
        visits=100,
        baseline_ratio=0.1,
        evidence_refs=("conversion-window",),
    )
    scaled_conversion = monitor.evaluate_conversion(
        service="checkout",
        onset_ts=_TS,
        conversions=2_000,
        visits=100_000,
        baseline_ratio=0.1,
        evidence_refs=("conversion-window",),
    )

    assert original_path.current == pytest.approx(scaled_path.current)
    assert original_path.relative_deformation == pytest.approx(scaled_path.relative_deformation)
    assert original_conversion.current == scaled_conversion.current
    assert original_conversion.relative_deformation == scaled_conversion.relative_deformation
    assert original_conversion.symptom is not None
    assert scaled_conversion.symptom is not None
    assert original_conversion.symptom.score == scaled_conversion.symptom.score


@given(
    values=st.lists(
        st.floats(
            min_value=0.001,
            max_value=10_000.0,
            allow_nan=False,
            allow_infinity=False,
        ),
        min_size=5,
        max_size=32,
    ),
    scale=st.integers(min_value=1, max_value=10_000),
)
def test_interarrival_cv_is_invariant_to_time_unit_or_rate_scaling(
    values: list[float], scale: int
) -> None:
    original = coefficient_of_variation(values)
    scaled = coefficient_of_variation([value * scale for value in values])

    assert original is not None
    assert scaled == pytest.approx(original, rel=1e-12, abs=1e-12)


def test_crowd_coherence_is_invariant_to_positive_kpi_rescaling() -> None:
    traffic = (1.0, 3.0, 2.0, 6.0, 4.0, 8.0)
    related = (2.0, 5.0, 4.0, 10.0, 7.0, 13.0)

    original = positive_pearson_coherence(traffic, related)
    scaled = positive_pearson_coherence(
        tuple(value * 100.0 for value in traffic),
        tuple(value * 0.25 for value in related),
    )

    assert original is not None
    assert scaled == pytest.approx(original, rel=1e-12, abs=1e-12)


def test_short_or_degenerate_sequences_are_insufficient_not_anomalous() -> None:
    monitor = _monitor()

    short_timing = monitor.evaluate_interarrival_variation(
        service="frontend",
        onset_ts=_TS,
        interarrival_seconds=(0.5, 1.0, 0.5, 1.0),
        baseline_cv=0.8,
        evidence_refs=("short-timing-window",),
    )
    constant_kpi = monitor.evaluate_crowd_coherence(
        service="frontend",
        onset_ts=_TS,
        traffic_values=(1.0, 2.0, 3.0, 4.0, 5.0),
        related_kpi_values=(2.0, 2.0, 2.0, 2.0, 2.0),
        baseline_coherence=0.9,
        evidence_refs=("constant-kpi-window",),
    )
    empty_conversion = monitor.evaluate_conversion(
        service="checkout",
        onset_ts=_TS,
        conversions=0,
        visits=0,
        baseline_ratio=0.1,
        evidence_refs=("empty-conversion-window",),
    )

    for result in (short_timing, constant_kpi, empty_conversion):
        assert result.current is None
        assert result.relative_deformation is None
        assert result.symptom is None


def test_extended_ratio_symptoms_retain_measured_evidence_values() -> None:
    result = _monitor().evaluate_crowd_coherence(
        service="frontend",
        onset_ts=_TS,
        traffic_values=(1.0, 2.0, 3.0, 4.0, 5.0),
        related_kpi_values=(1.0, 0.0, 1.0, 0.0, 1.0),
        baseline_coherence=0.9,
        evidence_refs=("checkout-kpi", "traffic-kpi"),
    )

    assert result.current == pytest.approx(0.0)
    assert result.symptom is not None
    assert result.symptom.signal == "crowd_coherence"
    assert result.symptom.evidence_refs == ("checkout-kpi", "traffic-kpi")
    assert "current=0" in result.symptom.note
    assert "baseline=0.9" in result.symptom.note
    assert "pairs=5, pearson=0" in result.symptom.note


@pytest.mark.parametrize(
    ("call", "message"),
    [
        (
            lambda monitor: monitor.evaluate_conversion(
                service="checkout",
                onset_ts=_TS,
                conversions=2,
                visits=1,
                baseline_ratio=0.1,
                evidence_refs=("conversion-window",),
            ),
            "conversions cannot exceed visits",
        ),
        (
            lambda monitor: monitor.evaluate_crowd_coherence(
                service="frontend",
                onset_ts=_TS,
                traffic_values=(1.0, 2.0, 3.0, 4.0, 5.0),
                related_kpi_values=(1.0, 2.0, 3.0),
                baseline_coherence=0.9,
                evidence_refs=("kpi-window",),
            ),
            "equal lengths",
        ),
        (
            lambda monitor: monitor.evaluate_interarrival_variation(
                service="frontend",
                onset_ts=_TS,
                interarrival_seconds=(1.0, 1.0, float("nan"), 1.0, 1.0),
                baseline_cv=0.8,
                evidence_refs=("timing-window",),
            ),
            "finite and non-negative",
        ),
    ],
)
def test_invalid_extended_ratio_evidence_fails_closed(call: object, message: str) -> None:
    with pytest.raises((TypeError, ValueError), match=message):
        call(_monitor())  # type: ignore[operator]


def _monitor() -> BehavioralRatioMonitor:
    return BehavioralRatioMonitor(configuration=behavioral_ratio_config())
