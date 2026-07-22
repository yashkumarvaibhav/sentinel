"""Low volume and missing telemetry remain distinct, event-time evidence paths."""

from __future__ import annotations

import ast
import inspect
from collections.abc import Callable
from datetime import UTC, datetime, timedelta, timezone

import pytest

from contracts import SymptomKind
from detection import liveness as liveness_module
from detection.liveness import LivenessDetector
from tests.factories import liveness_config

_TS = datetime(2026, 7, 22, 10, 0, tzinfo=UTC)


def test_low_volume_during_expected_high_emits_drop_with_measured_evidence() -> None:
    result = _detector().evaluate_drop(
        service="frontend",
        signal="request_rate",
        onset_ts=_TS,
        observed_value=20.0,
        expected_value=100.0,
        evidence_refs=("window-b", "window-a"),
    )

    assert result.observed_value == 20.0
    assert result.expected_value == 100.0
    assert result.relative_drop == pytest.approx(0.8)
    assert result.symptom is not None
    assert result.symptom.kind is SymptomKind.DROP
    assert result.symptom.service == "frontend"
    assert result.symptom.signal == "request_rate"
    assert result.symptom.onset_ts == _TS
    assert result.symptom.score == pytest.approx(8 / 9)
    assert result.symptom.evidence_refs == ("window-a", "window-b")
    assert "observed=20" in result.symptom.note
    assert "expected=100" in result.symptom.note
    assert "relative_drop=0.8" in result.symptom.note


def test_quiet_or_healthy_windows_and_missing_expectation_do_not_emit_drop() -> None:
    detector = _detector()

    quiet = detector.evaluate_drop(
        service="frontend",
        signal="request_rate",
        onset_ts=_TS,
        observed_value=0.0,
        expected_value=1.0,
        evidence_refs=("quiet-window",),
    )
    healthy = detector.evaluate_drop(
        service="frontend",
        signal="request_rate",
        onset_ts=_TS,
        observed_value=60.0,
        expected_value=100.0,
        evidence_refs=("healthy-window",),
    )
    missing = detector.evaluate_drop(
        service="frontend",
        signal="request_rate",
        onset_ts=_TS,
        observed_value=0.0,
        expected_value=None,
        evidence_refs=("unbased-window",),
    )
    unconfigured = detector.evaluate_drop(
        service="checkout",
        signal="request_rate",
        onset_ts=_TS,
        observed_value=0.0,
        expected_value=100.0,
        evidence_refs=("checkout-window",),
    )

    assert quiet.relative_drop is None
    assert quiet.symptom is None
    assert healthy.relative_drop == pytest.approx(0.4)
    assert healthy.symptom is None
    assert missing.relative_drop is None
    assert missing.symptom is None
    assert unconfigured.relative_drop is None
    assert unconfigured.symptom is None


def test_dead_emitter_fires_silence_from_supplied_event_time() -> None:
    result = _detector().evaluate_silence(
        service="frontend",
        signal="request_rate",
        expected_since_ts=_TS,
        last_seen_ts=_TS,
        watermark_ts=_TS + timedelta(minutes=5),
        evidence_refs=("watermark-300", "last-observation"),
    )

    assert result.reference_ts == _TS
    assert result.stale_age_seconds == 300.0
    assert result.symptom is not None
    assert result.symptom.kind is SymptomKind.SILENCE
    assert result.symptom.onset_ts == _TS + timedelta(minutes=2)
    assert result.symptom.score == 1.0
    assert result.symptom.evidence_refs == ("last-observation", "watermark-300")
    assert "last_seen=2026-07-22T10:00:00+00:00" in result.symptom.note
    assert "stale_age_seconds=300" in result.symptom.note
    assert "maximum_age_seconds=120" in result.symptom.note


def test_never_seen_emitter_is_silent_only_after_startup_grace() -> None:
    detector = _detector()
    fresh = detector.evaluate_silence(
        service="frontend",
        signal="request_rate",
        expected_since_ts=_TS,
        last_seen_ts=None,
        watermark_ts=_TS + timedelta(seconds=60),
        evidence_refs=("emitter-registration", "watermark-60"),
    )
    stale = detector.evaluate_silence(
        service="frontend",
        signal="request_rate",
        expected_since_ts=_TS,
        last_seen_ts=None,
        watermark_ts=_TS + timedelta(seconds=180),
        evidence_refs=("watermark-180", "emitter-registration"),
    )

    assert fresh.reference_ts == _TS
    assert fresh.stale_age_seconds == 60.0
    assert fresh.symptom is None
    assert stale.stale_age_seconds == 180.0
    assert stale.symptom is not None
    assert stale.symptom.onset_ts == _TS + timedelta(seconds=120)
    assert stale.symptom.score == pytest.approx(0.6)
    assert "last_seen=none" in stale.symptom.note


def test_fresh_emitter_and_unconfigured_stream_do_not_emit_silence() -> None:
    detector = _detector()
    watermark = _TS + timedelta(minutes=5)

    fresh = detector.evaluate_silence(
        service="frontend",
        signal="request_rate",
        expected_since_ts=_TS,
        last_seen_ts=watermark - timedelta(seconds=30),
        watermark_ts=watermark,
        evidence_refs=("fresh-observation", "watermark"),
    )
    unconfigured = detector.evaluate_silence(
        service="checkout",
        signal="request_rate",
        expected_since_ts=_TS,
        last_seen_ts=_TS,
        watermark_ts=watermark,
        evidence_refs=("checkout-observation", "watermark"),
    )

    assert fresh.stale_age_seconds == 30.0
    assert fresh.symptom is None
    assert unconfigured.stale_age_seconds is None
    assert unconfigured.symptom is None


def test_drop_and_silence_emit_at_their_exact_configured_boundaries() -> None:
    detector = _detector()
    drop = detector.evaluate_drop(
        service="frontend",
        signal="request_rate",
        onset_ts=_TS,
        observed_value=50.0,
        expected_value=100.0,
        evidence_refs=("boundary-window",),
    )
    silence = detector.evaluate_silence(
        service="frontend",
        signal="request_rate",
        expected_since_ts=_TS,
        last_seen_ts=_TS,
        watermark_ts=_TS + timedelta(seconds=120),
        evidence_refs=("boundary-observation", "boundary-watermark"),
    )

    assert drop.relative_drop == 0.5
    assert drop.symptom is not None
    assert silence.stale_age_seconds == 120.0
    assert silence.symptom is not None
    assert silence.symptom.onset_ts == _TS + timedelta(seconds=120)


def test_reference_reordering_preserves_drop_and_silence_identity() -> None:
    detector = _detector()
    first_drop = detector.evaluate_drop(
        service="frontend",
        signal="request_rate",
        onset_ts=_TS,
        observed_value=20.0,
        expected_value=100.0,
        evidence_refs=("b", "a"),
    )
    second_drop = detector.evaluate_drop(
        service="frontend",
        signal="request_rate",
        onset_ts=_TS,
        observed_value=20.0,
        expected_value=100.0,
        evidence_refs=("a", "b"),
    )
    first_silence = detector.evaluate_silence(
        service="frontend",
        signal="request_rate",
        expected_since_ts=_TS,
        last_seen_ts=_TS,
        watermark_ts=_TS + timedelta(minutes=5),
        evidence_refs=("b", "a"),
    )
    second_silence = detector.evaluate_silence(
        service="frontend",
        signal="request_rate",
        expected_since_ts=_TS,
        last_seen_ts=_TS,
        watermark_ts=_TS + timedelta(minutes=5),
        evidence_refs=("a", "b"),
    )

    assert first_drop == second_drop
    assert first_silence == second_silence


@pytest.mark.parametrize(
    ("operation", "message"),
    [
        (
            lambda: _detector().evaluate_drop(
                service="frontend",
                signal="request_rate",
                onset_ts=_TS,
                observed_value=-1.0,
                expected_value=100.0,
                evidence_refs=("window",),
            ),
            "observed_value must be finite and non-negative",
        ),
        (
            lambda: _detector().evaluate_drop(
                service="frontend",
                signal="request_rate",
                onset_ts=_TS,
                observed_value=0.0,
                expected_value=float("nan"),
                evidence_refs=("window",),
            ),
            "expected_value must be finite and non-negative",
        ),
        (
            lambda: _detector().evaluate_silence(
                service="frontend",
                signal="request_rate",
                expected_since_ts=_TS,
                last_seen_ts=_TS + timedelta(minutes=6),
                watermark_ts=_TS + timedelta(minutes=5),
                evidence_refs=("future-observation", "watermark"),
            ),
            "last_seen_ts cannot be after watermark_ts",
        ),
        (
            lambda: _detector().evaluate_silence(
                service="frontend",
                signal="request_rate",
                expected_since_ts=datetime(
                    2026,
                    7,
                    22,
                    10,
                    0,
                    tzinfo=timezone(timedelta(hours=1)),
                ),
                last_seen_ts=None,
                watermark_ts=_TS + timedelta(minutes=5),
                evidence_refs=("registration", "watermark"),
            ),
            "expected_since_ts must be timezone-aware UTC",
        ),
        (
            lambda: _detector().evaluate_silence(
                service="frontend",
                signal="request_rate",
                expected_since_ts=_TS,
                last_seen_ts=_TS,
                watermark_ts=_TS + timedelta(minutes=5),
                evidence_refs=("duplicate", "duplicate"),
            ),
            "evidence references must be unique",
        ),
    ],
)
def test_invalid_liveness_evidence_fails_closed(
    operation: Callable[[], object],
    message: str,
) -> None:
    with pytest.raises((TypeError, ValueError), match=message):
        operation()


def test_liveness_module_has_no_wall_clock_reads() -> None:
    tree = ast.parse(inspect.getsource(liveness_module))
    forbidden_calls = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in {"now", "time", "utcnow"}
    }

    assert forbidden_calls == set()


def _detector() -> LivenessDetector:
    return LivenessDetector(configuration=liveness_config())
