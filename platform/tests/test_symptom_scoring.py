"""Per-symptom scoring matches durable episodes without leaking private labels."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Literal, cast

import lab.scoring.capture as capture_scoring
import pytest
from lab.scenarios.models import ScoredSymptomKind, SymptomLabelInterval
from lab.scoring.evaluator import EpisodeRunScore, score_symptom_episodes
from lab.scoring.gates import evaluate_symptom_gates, load_gate_config
from lab.scoring.report import render_symptom_report

from common.config import load_config
from contracts import EpisodeStatus, SymptomEpisode, SymptomKind

START = datetime(2026, 7, 22, 12, 0, tzinfo=UTC)
REPO_ROOT = Path(__file__).resolve().parents[2]


def test_half_open_temporal_boundaries_do_not_manufacture_episode_matches() -> None:
    label = _label("expected-edge", SymptomKind.EDGE_DEGRADED, 10.0, 20.0)
    touching_before = _episode("before", SymptomKind.EDGE_DEGRADED, 0.0, 10.0)
    touching_after = _episode("after", SymptomKind.EDGE_DEGRADED, 20.0, 30.0)

    missed = score_symptom_episodes(
        capture_id="boundary-capture",
        scenario_id="cascade_night",
        seed=401,
        seed_purpose="development",
        anchor_ts=START,
        evaluation_end_ts=START + timedelta(seconds=40),
        episodes=(touching_after, touching_before),
        labels=(label,),
    ).score_for(SymptomKind.EDGE_DEGRADED)

    assert (missed.metrics.true_positive, missed.metrics.false_positive) == (0, 2)
    assert missed.metrics.false_negative == 1

    overlapping = _episode("overlap", SymptomKind.EDGE_DEGRADED, 19.0, 21.0)
    matched = score_symptom_episodes(
        capture_id="boundary-capture",
        scenario_id="cascade_night",
        seed=401,
        seed_purpose="development",
        anchor_ts=START,
        evaluation_end_ts=START + timedelta(seconds=40),
        episodes=(overlapping,),
        labels=(label,),
    ).score_for(SymptomKind.EDGE_DEGRADED)

    assert matched.metrics.true_positive == 1
    assert matched.metrics.false_positive == 0
    assert matched.metrics.false_negative == 0


def test_episode_matching_is_one_to_one_and_retry_revisions_are_deduplicated() -> None:
    label = _label("expected-edge", SymptomKind.EDGE_DEGRADED, 10.0, 20.0)
    active_retry = _episode(
        "episode-a",
        SymptomKind.EDGE_DEGRADED,
        11.0,
        None,
        revision=1,
    )
    closed_latest = _episode(
        "episode-a",
        SymptomKind.EDGE_DEGRADED,
        11.0,
        18.0,
        revision=2,
    )
    duplicate_prediction = _episode(
        "episode-b",
        SymptomKind.EDGE_DEGRADED,
        14.0,
        19.0,
    )

    score = score_symptom_episodes(
        capture_id="duplicate-capture",
        scenario_id="cascade_night",
        seed=401,
        seed_purpose="development",
        anchor_ts=START,
        evaluation_end_ts=START + timedelta(seconds=30),
        episodes=(closed_latest, duplicate_prediction, active_retry),
        labels=(label,),
    ).score_for(SymptomKind.EDGE_DEGRADED)

    assert score.predicted_count == 2
    assert score.expected_count == 1
    assert score.metrics.true_positive == 1
    assert score.metrics.false_positive == 1
    assert score.metrics.false_negative == 0
    assert score.metrics.precision.value == 0.5
    assert score.metrics.recall.value == 1.0


def test_missing_expected_and_predicted_kinds_keep_insufficient_denominators() -> None:
    edge_label = _label("expected-edge", SymptomKind.EDGE_DEGRADED, 10.0, 20.0)
    unexpected_log = _episode("unexpected-log", SymptomKind.LOG_BURST, 12.0, 18.0)

    score = score_symptom_episodes(
        capture_id="missing-kind-capture",
        scenario_id="cascade_night",
        seed=401,
        seed_purpose="development",
        anchor_ts=START,
        evaluation_end_ts=START + timedelta(seconds=30),
        episodes=(unexpected_log,),
        labels=(edge_label,),
    )
    edge = score.score_for(SymptomKind.EDGE_DEGRADED)
    log = score.score_for(SymptomKind.LOG_BURST)
    residual = score.score_for(SymptomKind.RESIDUAL_EXCEED)

    assert edge.metrics.precision.status == "insufficient"
    assert edge.metrics.recall.value == 0.0
    assert log.metrics.precision.value == 0.0
    assert log.metrics.recall.status == "insufficient"
    assert residual.metrics.precision.status == "insufficient"
    assert residual.metrics.recall.status == "insufficient"


def test_duplicate_private_label_ids_fail_closed() -> None:
    label = _label("duplicate-label", SymptomKind.EDGE_DEGRADED, 10.0, 20.0)

    with pytest.raises(ValueError, match="symptom label IDs must be unique"):
        score_symptom_episodes(
            capture_id="duplicate-label-capture",
            scenario_id="cascade_night",
            seed=401,
            seed_purpose="development",
            anchor_ts=START,
            evaluation_end_ts=START + timedelta(seconds=30),
            episodes=(),
            labels=(label, label),
        )


def test_symptom_gates_fail_closed_on_recall_only_never_precision() -> None:
    score = score_symptom_episodes(
        capture_id="gate-capture",
        scenario_id="cascade_night",
        seed=401,
        seed_purpose="development",
        anchor_ts=START,
        evaluation_end_ts=START + timedelta(seconds=30),
        episodes=(_episode("unexpected-log", SymptomKind.LOG_BURST, 12.0, 18.0),),
        labels=(_label("expected-edge", SymptomKind.EDGE_DEGRADED, 10.0, 20.0),),
    )
    config = load_gate_config(REPO_ROOT / "lab" / "scoring" / "config.yml")

    result = evaluate_symptom_gates(
        (score,),
        config,
        required_kinds=(
            SymptomKind.RESIDUAL_EXCEED,
            SymptomKind.LOG_BURST,
            SymptomKind.EDGE_DEGRADED,
        ),
    )

    # Only recall gates: RESIDUAL/LOG_BURST have no predicted match (recall insufficient),
    # EDGE_DEGRADED has an expected label with zero coverage (recall 0.0). No precision
    # failure ever appears, even though LOG_BURST emitted a spurious episode (precision 0.0).
    assert not result.passed
    assert {failure.metric for failure in result.failures} == {"symptom_recall"}
    assert {failure.scope for failure in result.failures} == {
        "RESIDUAL_EXCEED",
        "LOG_BURST",
        "EDGE_DEGRADED",
    }


def test_low_precision_high_recall_passes_because_precision_is_not_gated() -> None:
    # One labeled edge fault, caught by its direct episode AND a propagated one:
    # recall 1.0, precision 0.5. Phase 2 must PASS -- the storm is Phase 4's to collapse.
    score = score_symptom_episodes(
        capture_id="storm-capture",
        scenario_id="cascade_night",
        seed=401,
        seed_purpose="development",
        anchor_ts=START,
        evaluation_end_ts=START + timedelta(seconds=40),
        episodes=(
            _episode("edge-direct", SymptomKind.EDGE_DEGRADED, 10.0, 30.0),
            _propagated_edge("edge-prop", 11.0, 30.0),
        ),
        labels=(_label("expected-edge", SymptomKind.EDGE_DEGRADED, 10.0, 20.0),),
    )
    config = load_gate_config(REPO_ROOT / "lab" / "scoring" / "config.yml")
    edge = score.score_for(SymptomKind.EDGE_DEGRADED)
    assert edge.metrics.recall.value == 1.0
    assert edge.metrics.precision.value == 0.5

    result = evaluate_symptom_gates((score,), config, required_kinds=(SymptomKind.EDGE_DEGRADED,))

    assert result.passed
    assert result.failures == ()


def test_capture_scorer_opens_private_labels_only_after_runtime_replay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    replay = SimpleNamespace(
        capture_id="ordered-capture",
        scenario_id="cascade_night",
        seed=401,
        seed_purpose="development",
        anchor_ts=START,
        steps=(SimpleNamespace(tick_ts=START + timedelta(seconds=1), results=()),),
        active_episodes=(),
    )

    def replay_public(*_: object, **__: object) -> object:
        calls.append("runtime-replay")
        return replay

    def read_labels(_: Path) -> bytes:
        assert calls == ["runtime-replay"]
        calls.append("private-labels")
        return (
            b'{"intervals":[],"scenario_id":"cascade_night","seed":401,'
            b'"seed_purpose":"development","symptom_intervals":[],"version":1}'
        )

    monkeypatch.setattr(capture_scoring, "load_runtime_capture", lambda _: object())
    monkeypatch.setattr(capture_scoring, "replay_edge_detection", replay_public)
    monkeypatch.setattr(capture_scoring, "load_private_labels", read_labels)
    config = load_config(REPO_ROOT / "config")

    score = capture_scoring.score_edge_episode_capture(
        Path("unused"),
        detector=config.detectors,
        replay_config_fingerprint=config.fingerprint,
    )

    assert calls == ["runtime-replay", "private-labels"]
    assert score.score_for(SymptomKind.EDGE_DEGRADED).metrics.precision.status == "insufficient"


def test_combined_capture_scorer_finishes_every_public_path_before_labels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    manifest = SimpleNamespace(
        telemetry=SimpleNamespace(
            logical_service="frontend",
            logical_signal="request_rate",
            tick_seconds=2,
        )
    )
    identity = {
        "capture_id": "combined-capture",
        "scenario_id": "cascade_night",
        "seed": 401,
        "seed_purpose": "development",
        "anchor_ts": START,
    }
    decomposition = SimpleNamespace(
        **identity,
        steps=(SimpleNamespace(frame=None),),
    )
    detector_replay = SimpleNamespace(
        **identity,
        steps=(SimpleNamespace(results=()),),
        active_episodes=(),
    )

    def public_replay(name: str, result: object) -> object:
        calls.append(name)
        return result

    def read_labels(_: Path) -> bytes:
        assert calls == ["decomposition", "edge", "logs", "ratios", "liveness", "resources"]
        calls.append("private-labels")
        return (
            b'{"intervals":[],"scenario_id":"cascade_night","seed":401,'
            b'"seed_purpose":"development","symptom_intervals":[],"version":1}'
        )

    monkeypatch.setattr(
        capture_scoring, "load_runtime_capture", lambda _: SimpleNamespace(manifest=manifest)
    )
    monkeypatch.setattr(
        capture_scoring,
        "replay_decomposition",
        lambda *_args, **_kwargs: public_replay("decomposition", decomposition),
    )
    monkeypatch.setattr(
        capture_scoring,
        "replay_edge_detection",
        lambda *_args, **_kwargs: public_replay("edge", detector_replay),
    )
    monkeypatch.setattr(
        capture_scoring,
        "replay_log_detection",
        lambda *_args, **_kwargs: public_replay("logs", detector_replay),
    )
    monkeypatch.setattr(
        capture_scoring,
        "replay_ingress_ratios",
        lambda *_args, **_kwargs: public_replay("ratios", detector_replay),
    )
    monkeypatch.setattr(
        capture_scoring,
        "replay_liveness_detection",
        lambda *_args, **_kwargs: public_replay("liveness", detector_replay),
    )
    monkeypatch.setattr(
        capture_scoring,
        "replay_resource_detection",
        lambda *_args, **_kwargs: public_replay("resources", detector_replay),
    )
    monkeypatch.setattr(capture_scoring, "load_private_labels", read_labels)
    config = load_config(REPO_ROOT / "config")

    score = capture_scoring.score_detection_episode_capture(
        Path("unused"),
        detector=config.detectors,
        replay_config_fingerprint=config.fingerprint,
    )

    assert calls[-1] == "private-labels"
    assert all(item.metrics.precision.status == "insufficient" for item in score.by_kind)


def _label(
    label_id: str,
    kind: SymptomKind,
    start: float,
    end: float,
) -> SymptomLabelInterval:
    route = {
        SymptomKind.EDGE_DEGRADED: ("checkout", "dependency.payment"),
        SymptomKind.LOG_BURST: ("payment", "log_template_rate"),
        SymptomKind.RESIDUAL_EXCEED: ("frontend", "request_rate"),
    }
    service, signal = route[kind]
    return SymptomLabelInterval(
        label_id=label_id,
        kind=cast(ScoredSymptomKind, kind.value),
        service=service,
        signal=signal,
        start_offset_seconds=start,
        end_offset_seconds=end,
    )


def _episode(
    episode_id: str,
    kind: SymptomKind,
    opened: float,
    closed: float | None,
    *,
    revision: int = 1,
) -> SymptomEpisode:
    route = {
        SymptomKind.EDGE_DEGRADED: ("checkout", "dependency.payment"),
        SymptomKind.LOG_BURST: ("payment", "log_template_rate"),
        SymptomKind.RESIDUAL_EXCEED: ("frontend", "request_rate"),
    }
    service, signal = route[kind]
    opened_ts = START + timedelta(seconds=opened)
    confirmed_ts = opened_ts + timedelta(seconds=1)
    last_breach_ts = confirmed_ts
    closed_ts = None if closed is None else START + timedelta(seconds=closed)
    return SymptomEpisode(
        episode_id=episode_id,
        kind=kind,
        service=service,
        signal=signal,
        status=EpisodeStatus.ACTIVE if closed is None else EpisodeStatus.CLOSED,
        opened_ts=opened_ts,
        confirmed_ts=confirmed_ts,
        last_breach_ts=last_breach_ts,
        closed_ts=closed_ts,
        peak_score=0.9,
        breach_tick_count=3,
        revision=revision,
        opening_symptom_id=f"{episode_id}-open",
        peak_symptom_id=f"{episode_id}-peak",
        latest_symptom_id=f"{episode_id}-latest",
        evidence_refs=(f"{episode_id}-evidence",),
    )


def _propagated_edge(
    episode_id: str,
    opened: float,
    closed: float | None,
) -> SymptomEpisode:
    """A frontend->checkout edge episode: one topology hop up from checkout->payment."""
    opened_ts = START + timedelta(seconds=opened)
    confirmed_ts = opened_ts + timedelta(seconds=1)
    closed_ts = None if closed is None else START + timedelta(seconds=closed)
    return SymptomEpisode(
        episode_id=episode_id,
        kind=SymptomKind.EDGE_DEGRADED,
        service="frontend",
        signal="dependency.checkout",
        status=EpisodeStatus.ACTIVE if closed is None else EpisodeStatus.CLOSED,
        opened_ts=opened_ts,
        confirmed_ts=confirmed_ts,
        last_breach_ts=confirmed_ts,
        closed_ts=closed_ts,
        peak_score=0.9,
        breach_tick_count=3,
        revision=1,
        opening_symptom_id=f"{episode_id}-open",
        peak_symptom_id=f"{episode_id}-peak",
        latest_symptom_id=f"{episode_id}-latest",
        evidence_refs=(f"{episode_id}-evidence",),
    )


def test_combo_development_scoring_requires_both_base_capture_paths() -> None:
    with pytest.raises(SystemExit) as excinfo:
        capture_scoring.main(
            [
                "--repo-root",
                str(REPO_ROOT),
                "--development-combo-capture",
                "var/captures/phase2-combo-503-dev-v10",
                "--report",
                "var/reports/never-written.md",
            ]
        )
    assert excinfo.value.code == 2


def _held_out_run(
    scenario_id: str,
    seed: int,
    purpose: Literal["development", "held_out"] = "held_out",
) -> EpisodeRunScore:
    return EpisodeRunScore(
        capture_id=f"{scenario_id}-{seed}",
        scenario_id=scenario_id,
        seed=seed,
        seed_purpose=purpose,
        evaluation_start_ts=START,
        evaluation_end_ts=START + timedelta(seconds=30),
        by_kind=(),
    )


def test_held_out_symptom_matrix_is_the_sealed_cascade_and_combo_seeds() -> None:
    assert capture_scoring._held_out_symptom_matrix(REPO_ROOT) == frozenset(
        {
            ("cascade_night", 9103),
            ("cascade_night", 9127),
            ("combo_night", 9209),
            ("combo_night", 9221),
        }
    )


def test_held_out_symptom_matrix_accepts_the_exact_sealed_set() -> None:
    expected = capture_scoring._held_out_symptom_matrix(REPO_ROOT)
    runs = tuple(_held_out_run(scenario, seed) for scenario, seed in expected)
    capture_scoring._require_held_out_symptom_matrix(runs, expected)


def test_held_out_symptom_matrix_rejects_a_development_capture() -> None:
    expected = capture_scoring._held_out_symptom_matrix(REPO_ROOT)
    runs = tuple(
        _held_out_run(scenario, seed, "development" if seed == 9103 else "held_out")
        for scenario, seed in expected
    )
    with pytest.raises(ValueError, match="cannot consume development"):
        capture_scoring._require_held_out_symptom_matrix(runs, expected)


def test_held_out_symptom_matrix_rejects_an_incomplete_seed_set() -> None:
    expected = capture_scoring._held_out_symptom_matrix(REPO_ROOT)
    runs = tuple(
        _held_out_run(scenario, seed)
        for scenario, seed in expected
        if (scenario, seed) != ("combo_night", 9221)
    )
    with pytest.raises(ValueError, match="missing="):
        capture_scoring._require_held_out_symptom_matrix(runs, expected)


def test_held_out_symptom_report_marks_the_sealed_scope() -> None:
    score = score_symptom_episodes(
        capture_id="held-out-capture",
        scenario_id="combo_night",
        seed=9209,
        seed_purpose="held_out",
        anchor_ts=START,
        evaluation_end_ts=START + timedelta(seconds=30),
        episodes=(_episode("edge", SymptomKind.EDGE_DEGRADED, 10.0, 20.0),),
        labels=(_label("edge", SymptomKind.EDGE_DEGRADED, 10.0, 20.0),),
    )
    config = load_gate_config(REPO_ROOT / "lab" / "scoring" / "config.yml")
    gate = evaluate_symptom_gates((score,), config)

    report = render_symptom_report(
        runs=(score,),
        gate=gate,
        config=config,
        config_fingerprint="held-out-fingerprint",
        mode="held_out",
    )

    assert "Held-out gate:" in report
    assert "**HELD-OUT**" in report
    assert "Held-out captures" in report
    assert "Phase 2 closure" in report
    assert "DEVELOPMENT** captures only" not in report


def test_held_out_symptom_mode_rejects_development_flags() -> None:
    with pytest.raises(SystemExit) as excinfo:
        capture_scoring.main(
            [
                "--repo-root",
                str(REPO_ROOT),
                "--held-out-symptom-captures-root",
                "var/captures",
                "--development-combo-capture",
                "var/captures/x",
                "--report",
                "var/reports/never-written.md",
            ]
        )
    assert excinfo.value.code == 2
