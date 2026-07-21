"""Drain3 mining stays replay-stable and log storms emit measured burst evidence."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from contracts import SymptomKind
from detection.logs import LogLine, LogTemplateBurstDetector
from tests.factories import log_template_config

_TS = datetime(2026, 7, 21, 16, 0, tzinfo=UTC)


def test_variable_tokens_share_one_stable_template_independent_of_input_order() -> None:
    records = _login_lines(count=8, start=_TS)
    first = _detector().mine_window(records, window_seconds=60.0)
    second = _detector().mine_window(tuple(reversed(records)), window_seconds=60.0)

    assert first == second
    assert len(first) == 1
    assert first[0].count == 8
    assert first[0].evidence_refs == tuple(record.log_id for record in records)
    assert "<*>" in first[0].template
    assert "user-0" not in first[0].template
    assert "10.0.0.1" not in first[0].template


def test_template_identity_is_service_scoped() -> None:
    message = "connection reset by peer 10.0.0.1 port 9000"
    detector = _detector()

    frequencies = detector.mine_window(
        (
            _line("frontend-log", 0, message, service="frontend"),
            _line("checkout-log", 1, message, service="checkout"),
        ),
        window_seconds=60.0,
    )

    assert len(frequencies) == 2
    assert frequencies[0].service != frequencies[1].service
    assert frequencies[0].template_id != frequencies[1].template_id


def test_seeded_syslog_storm_emits_one_log_burst_with_frequency_evidence() -> None:
    detector = _detector()
    baseline = detector.mine_window(_login_lines(count=6, start=_TS), window_seconds=60.0)
    assert len(baseline) == 1
    baseline_rates = {baseline[0].template_id: baseline[0].messages_per_second}
    storm = _login_lines(count=60, start=_TS + timedelta(minutes=1), id_prefix="storm")

    result = detector.detect_window(
        storm,
        window_seconds=60.0,
        baseline_rates=baseline_rates,
    )

    assert len(result.frequencies) == 1
    assert len(result.symptoms) == 1
    frequency = result.frequencies[0]
    symptom = result.symptoms[0]
    assert frequency.count == 60
    assert frequency.messages_per_second == 1.0
    assert symptom.kind is SymptomKind.LOG_BURST
    assert symptom.signal == "log_template_rate"
    assert symptom.score == pytest.approx(0.9)
    assert symptom.onset_ts == storm[0].ts
    assert symptom.evidence_refs == tuple(record.log_id for record in storm)
    assert f"template_id={frequency.template_id}" in symptom.note
    assert "current_per_second=1" in symptom.note
    assert "baseline_per_second=0.1" in symptom.note
    assert "count=60" in symptom.note
    assert "relative_deformation=9" in symptom.note


def test_normal_frequency_and_subminimum_new_template_do_not_emit() -> None:
    detector = _detector()
    normal = _login_lines(count=6, start=_TS)
    mined = detector.mine_window(normal, window_seconds=60.0)
    baseline_rates = {mined[0].template_id: mined[0].messages_per_second}

    same_rate = detector.detect_window(
        _login_lines(count=6, start=_TS + timedelta(minutes=1), id_prefix="same"),
        window_seconds=60.0,
        baseline_rates=baseline_rates,
    )
    rare = detector.detect_window(
        (_line("rare-1", 120, "kernel panic signature alpha in isolated worker"),),
        window_seconds=60.0,
        baseline_rates=baseline_rates,
    )

    assert same_rate.symptoms == ()
    assert len(rare.frequencies) == 1
    assert rare.frequencies[0].count == 1
    assert rare.symptoms == ()


def test_reprocessing_the_same_window_preserves_detection_output() -> None:
    detector = _detector()
    storm = _login_lines(count=12, start=_TS, id_prefix="retry")

    first = detector.detect_window(storm, window_seconds=60.0, baseline_rates={})
    second = detector.detect_window(storm, window_seconds=60.0, baseline_rates={})

    assert first == second
    assert len(first.symptoms) == 1


@pytest.mark.parametrize(
    ("operation", "message"),
    [
        (
            lambda: LogLine(
                log_id="local-time",
                ts=datetime(2026, 7, 21, 17, 0, tzinfo=timezone(timedelta(hours=1))),
                service="frontend",
                message="hello",
            ),
            "timezone-aware UTC",
        ),
        (
            lambda: _detector().mine_window(
                (
                    _line("duplicate", 0, "first message"),
                    _line("duplicate", 1, "second message"),
                ),
                window_seconds=60.0,
            ),
            "log_id values must be unique",
        ),
        (
            lambda: _detector().mine_window((), window_seconds=0.0),
            "greater than zero",
        ),
        (
            lambda: _detector().detect_window(
                (),
                window_seconds=60.0,
                baseline_rates={"template": float("nan")},
            ),
            "finite and non-negative",
        ),
    ],
)
def test_invalid_log_windows_fail_closed(operation: object, message: str) -> None:
    with pytest.raises((TypeError, ValueError), match=message):
        operation()  # type: ignore[operator]


def _detector() -> LogTemplateBurstDetector:
    return LogTemplateBurstDetector(configuration=log_template_config())


def _login_lines(
    *,
    count: int,
    start: datetime,
    id_prefix: str = "baseline",
) -> tuple[LogLine, ...]:
    return tuple(
        LogLine(
            log_id=f"{id_prefix}-{index:03d}",
            ts=start + timedelta(milliseconds=index),
            service="frontend",
            message=(
                f"sshd[{1000 + index}]: Failed password for invalid user user-{index} "
                f"from 10.0.0.{index + 1} port {51000 + index} ssh2"
            ),
        )
        for index in range(count)
    )


def _line(
    log_id: str,
    offset_seconds: int,
    message: str,
    *,
    service: str = "frontend",
) -> LogLine:
    return LogLine(
        log_id=log_id,
        ts=_TS + timedelta(seconds=offset_seconds),
        service=service,
        message=message,
    )
