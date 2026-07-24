"""Concept-drift monitoring: injected-drift detection, no false alarms, board isolation."""

from __future__ import annotations

import random
from pathlib import Path

import pytest

from ml.config import DriftParamsConfig, load_drift_params
from ml.drift import DriftBoard, StreamDriftMonitor, run_drift_demo

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_ROOT = REPO_ROOT / "config"


@pytest.fixture(scope="module")
def params() -> DriftParamsConfig:
    return load_drift_params(CONFIG_ROOT / "ml-drift.yml")


def test_injected_drift_is_detected_without_false_alarms(params: DriftParamsConfig) -> None:
    reports = run_drift_demo(params)
    assert {report.stream for report in reports} == set(params.streams)
    for report in reports:
        assert report.stable_false_alarms == 0
        assert report.drift_detected
        assert report.first_drift_index is not None


def test_demo_is_deterministic(params: DriftParamsConfig) -> None:
    first = run_drift_demo(params)
    second = run_drift_demo(params)
    assert [(r.stream, r.first_drift_index) for r in first] == [
        (r.stream, r.first_drift_index) for r in second
    ]


def test_kswin_detector_also_detects(params: DriftParamsConfig) -> None:
    kswin = params.model_copy(update={"detector": "kswin"})
    reports = run_drift_demo(kswin)
    assert all(report.detector == "kswin" for report in reports)
    assert all(report.drift_detected for report in reports)


def test_stream_monitor_quiet_then_fires(params: DriftParamsConfig) -> None:
    monitor = StreamDriftMonitor("residual_score", params)
    rng = random.Random(1)
    stable_alarms = [monitor.observe(rng.gauss(0.0, 1.0)) for _ in range(300)]
    assert all(alarm is None for alarm in stable_alarms)
    fired = [monitor.observe(rng.gauss(4.0, 1.0)) for _ in range(300)]
    assert any(alarm is not None for alarm in fired)


def test_board_isolates_drift_to_the_shifted_stream(params: DriftParamsConfig) -> None:
    board = DriftBoard(params)
    rng = random.Random(2)
    for _ in range(300):
        board.observe({stream: rng.gauss(0.0, 1.0) for stream in params.streams})
    for _ in range(300):
        values = {stream: rng.gauss(0.0, 1.0) for stream in params.streams}
        values["residual_score"] = rng.gauss(4.0, 1.0)
        board.observe(values)
    alarmed = {alarm.stream for alarm in board.alarms}
    assert "residual_score" in alarmed
    assert "anomaly_score" not in alarmed


def test_board_ignores_unknown_streams(params: DriftParamsConfig) -> None:
    board = DriftBoard(params)
    assert board.observe({"not_a_configured_stream": 5.0}) == ()
    assert board.alarms == ()
