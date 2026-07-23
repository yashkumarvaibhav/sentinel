"""Episode characterization dumps the label-free prediction stream, no labels."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import lab.scoring.capture as capture_scoring
import pytest
from lab.scoring.capture import DetectionEpisodeReplay
from lab.scoring.diagnose import (
    characterize_capture,
    characterize_replay,
    render_characterization,
)

from common.config import load_config
from contracts import EpisodeStatus, SymptomEpisode, SymptomKind

START = datetime(2026, 7, 22, 12, 0, tzinfo=UTC)
REPO_ROOT = Path(__file__).resolve().parents[2]


def _episode(
    episode_id: str,
    kind: SymptomKind,
    service: str,
    signal: str,
    opened: float,
    closed: float | None,
    *,
    revision: int = 1,
    peak_score: float = 0.9,
) -> SymptomEpisode:
    opened_ts = START + timedelta(seconds=opened)
    confirmed_ts = opened_ts + timedelta(seconds=1)
    closed_ts = None if closed is None else START + timedelta(seconds=closed)
    return SymptomEpisode(
        episode_id=episode_id,
        kind=kind,
        service=service,
        signal=signal,
        status=EpisodeStatus.ACTIVE if closed is None else EpisodeStatus.CLOSED,
        opened_ts=opened_ts,
        confirmed_ts=confirmed_ts,
        last_breach_ts=confirmed_ts,
        closed_ts=closed_ts,
        peak_score=peak_score,
        breach_tick_count=3,
        revision=revision,
        opening_symptom_id=f"{episode_id}-open",
        peak_symptom_id=f"{episode_id}-peak",
        latest_symptom_id=f"{episode_id}-latest",
        evidence_refs=(f"{episode_id}-evidence",),
    )


def _replay(episodes: tuple[SymptomEpisode, ...]) -> DetectionEpisodeReplay:
    return DetectionEpisodeReplay(
        capture_id="phase2-combo-503-dev-vX",
        scenario_id="combo_night",
        seed=503,
        seed_purpose="development",
        anchor_ts=START,
        evaluation_end_ts=START + timedelta(seconds=600),
        logical_service="frontend",
        logical_signal="request_rate",
        episodes=episodes,
    )


def test_characterization_groups_orders_and_counts_every_kind() -> None:
    # Two EDGE episodes (direct + propagated) is exactly the storm shape 6a studies.
    direct = _episode(
        "edge-direct", SymptomKind.EDGE_DEGRADED, "checkout", "dependency.payment", 90.0, 130.0
    )
    propagated = _episode(
        "edge-prop", SymptomKind.EDGE_DEGRADED, "frontend", "dependency.checkout", 95.0, None
    )
    residual = _episode("res", SymptomKind.RESIDUAL_EXCEED, "frontend", "request_rate", 10.0, 40.0)

    result = characterize_replay(_replay((propagated, residual, direct)))

    # Ordered by (kind.value, opened_ts, episode_id): EDGE_DEGRADED before RESIDUAL_EXCEED.
    assert [record.episode_id for record in result.records] == [
        "edge-direct",
        "edge-prop",
        "res",
    ]
    edge_direct = result.records[0]
    assert (edge_direct.opened_offset_seconds, edge_direct.confirmed_offset_seconds) == (90.0, 91.0)
    assert edge_direct.closed_offset_seconds == 130.0
    assert result.records[1].closed_offset_seconds is None  # active episode
    counts = dict(result.counts_by_kind())
    assert counts[SymptomKind.EDGE_DEGRADED] == 2
    assert counts[SymptomKind.RESIDUAL_EXCEED] == 1
    assert counts[SymptomKind.SATURATION] == 0  # zero kinds still reported
    assert len(counts) == 7  # every scored kind is reported, present or not


def test_characterization_collapses_to_latest_revision() -> None:
    opened = _episode(
        "edge", SymptomKind.EDGE_DEGRADED, "checkout", "dependency.payment", 90.0, None, revision=1
    )
    closed = _episode(
        "edge", SymptomKind.EDGE_DEGRADED, "checkout", "dependency.payment", 90.0, 130.0, revision=2
    )

    result = characterize_replay(_replay((opened, closed)))

    assert len(result.records) == 1
    assert result.records[0].closed_offset_seconds == 130.0
    assert result.records[0].status is EpisodeStatus.CLOSED


def test_characterize_capture_never_opens_private_labels(monkeypatch: pytest.MonkeyPatch) -> None:
    replay = _replay(
        (_episode("res", SymptomKind.RESIDUAL_EXCEED, "frontend", "request_rate", 10.0, 40.0),)
    )
    monkeypatch.setattr(
        "lab.scoring.diagnose.replay_detection_episodes",
        lambda *_args, **_kwargs: replay,
    )

    def _forbidden(_: Path) -> bytes:
        raise AssertionError("characterization must not read private labels")

    monkeypatch.setattr(capture_scoring, "load_private_labels", _forbidden)
    config = load_config(REPO_ROOT / "config")

    result = characterize_capture(
        Path("unused"),
        detector=config.detectors,
        replay_config_fingerprint=config.fingerprint,
    )

    assert result.capture_id == "phase2-combo-503-dev-vX"


def test_render_is_deterministic_and_declares_honesty() -> None:
    config = load_config(REPO_ROOT / "config")
    result = characterize_replay(
        _replay(
            (
                _episode(
                    "edge", SymptomKind.EDGE_DEGRADED, "checkout", "dependency.payment", 90.0, 130.0
                ),
            )
        )
    )

    first = render_characterization(
        (result,), topology=config.topology, config_fingerprint=config.fingerprint
    )
    second = render_characterization(
        (result,), topology=config.topology, config_fingerprint=config.fingerprint
    )

    assert first == second
    assert "No private label is read" in first
    assert config.fingerprint in first
    assert "REAL" in first and "SIMULATED" in first
    # Topology context is present so propagation is traceable by hand.
    assert "`checkout`" in first and "`payment`" in first
    # The two-kind count line surfaces per-kind totals including zeros.
    assert "EDGE_DEGRADED 1" in first and "SATURATION 0" in first


def test_render_reports_empty_capture_without_a_table() -> None:
    config = load_config(REPO_ROOT / "config")
    result = characterize_replay(_replay(()))

    report = render_characterization(
        (result,), topology=config.topology, config_fingerprint=config.fingerprint
    )

    assert "No episodes emitted." in report
    assert "| kind |" not in report
