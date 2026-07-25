"""Measured proof: drive a real development capture through the decision plane."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from lab.captures import load_runtime_capture
from lab.scoring.decisions import (
    CAPTURE_COVERED_KINDS,
    DecisionReplay,
    covered_services,
    load_decision_configs,
    render_decision_report,
    replay_capture_decisions,
    semantic_transcript,
)

from common.config import load_config
from contracts import AgentStatus, EvidenceAxis, SymptomKind

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_ROOT = REPO_ROOT / "config"
CAPTURE = REPO_ROOT / "var" / "captures" / "phase2-cascade-401-dev-v6"


def test_the_capture_coverage_claim_names_every_runtime_kind_but_no_change_feed() -> None:
    assert SymptomKind.DEPLOY_MARKER not in CAPTURE_COVERED_KINDS
    assert SymptomKind.EDGE_DEGRADED in CAPTURE_COVERED_KINDS
    assert len(CAPTURE_COVERED_KINDS) == 7


@pytest.fixture(scope="module")
def replay() -> DecisionReplay:
    if not CAPTURE.is_dir():
        pytest.skip("local development capture phase2-cascade-401-dev-v6 is not present")
    return replay_capture_decisions(
        CAPTURE,
        config=load_config(CONFIG_ROOT),
        decisions=load_decision_configs(CONFIG_ROOT),
    )


def test_telemetry_coverage_is_measured_from_the_capture_not_assumed() -> None:
    if not CAPTURE.is_dir():
        pytest.skip("local development capture phase2-cascade-401-dev-v6 is not present")
    config = load_config(CONFIG_ROOT)

    services = covered_services(load_runtime_capture(CAPTURE), detector=config.detectors)

    # Real services the demo mesh emits, under the names the detectors use.
    assert {"frontend", "checkout", "payment"} <= services
    # Nothing is invented: a service the mesh does not run is not covered.
    assert "nonexistent-service" not in services


def test_a_real_storm_becomes_one_verified_incident(replay: DecisionReplay) -> None:
    assert replay.seed_purpose == "development"
    assert replay.ticks, "a capture with durable episodes must produce decision ticks"
    for tick in replay.ticks:
        assert len(tick.outcomes) == 1
    peak = max(replay.ticks, key=lambda tick: tick.assessment(EvidenceAxis.RELIABILITY).score)
    assert peak.assessment(EvidenceAxis.RELIABILITY).score > 0.5
    assert peak.outcomes[0].verification.confirmed is True


def test_the_change_axis_stays_insufficient_without_a_feed(replay: DecisionReplay) -> None:
    for tick in replay.ticks:
        assert tick.assessment(EvidenceAxis.CHANGE_CONFIG).status is AgentStatus.INSUFFICIENT


def test_the_transcript_is_canonical_and_clock_free(replay: DecisionReplay) -> None:
    transcript = semantic_transcript(replay)

    assert semantic_transcript(replay) == transcript
    value = json.loads(transcript)
    assert value["seed_purpose"] == "development"
    assert value["ticks"], "a replay with ticks must transcribe them"
    assert all("offset_seconds" in tick for tick in value["ticks"])
    # An absolute timestamp would make the transcript unusable as a golden.
    assert b"+00:00" not in transcript


def test_the_report_states_the_configuration_it_judged_under(replay: DecisionReplay) -> None:
    config = load_config(CONFIG_ROOT)
    decisions = load_decision_configs(CONFIG_ROOT)

    report = render_decision_report(
        (replay,),
        config_fingerprint=config.fingerprint,
        decision_fingerprint=decisions.fingerprint,
    )

    assert config.fingerprint in report
    assert decisions.fingerprint in report
    assert "No private label is read" in report
