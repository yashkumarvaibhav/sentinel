"""Episodes open only on persistence, close only on hysteresis, and never flap."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from hypothesis import given
from hypothesis import strategies as st

from common.config import EpisodeConfig, EpisodePolicyConfig
from contracts import EpisodeStatus, Symptom, SymptomEpisode, SymptomKind
from detection.episodes import (
    EpisodeAction,
    EpisodeKey,
    EpisodePhase,
    SymptomEpisodeMachine,
)

_TS = datetime(2026, 7, 22, 12, 0, tzinfo=UTC)
_KIND = SymptomKind.RESIDUAL_EXCEED


def _policy(
    *,
    open_after_ticks: int = 3,
    close_after_ticks: int = 3,
    breach_score: float = 0.5,
    clear_score: float = 0.2,
) -> EpisodePolicyConfig:
    return EpisodePolicyConfig(
        open_after_ticks=open_after_ticks,
        close_after_ticks=close_after_ticks,
        breach_score=breach_score,
        clear_score=clear_score,
    )


def _machine(policy: EpisodePolicyConfig | None = None) -> SymptomEpisodeMachine:
    return SymptomEpisodeMachine(
        configuration=EpisodeConfig(policies={_KIND.value: policy or _policy()})
    )


def _key(kind: SymptomKind = _KIND) -> EpisodeKey:
    return EpisodeKey(kind, "frontend", "request_rate")


def _symptom(
    index: int,
    score: float,
    *,
    kind: SymptomKind = _KIND,
    service: str = "frontend",
    signal: str = "request_rate",
) -> Symptom:
    return Symptom(
        symptom_id=f"symptom-{index}",
        kind=kind,
        service=service,
        signal=signal,
        onset_ts=_TS + timedelta(seconds=index),
        score=score,
        note="residual exceeded the context-aware band",
        evidence_refs=(f"frame-{index}",),
    )


def _run(
    machine: SymptomEpisodeMachine,
    scores: list[float],
    *,
    key: EpisodeKey | None = None,
    start: int = 0,
) -> list[tuple[EpisodeAction, SymptomEpisode | None]]:
    target = key or _key()
    outcomes: list[tuple[EpisodeAction, SymptomEpisode | None]] = []
    for offset, score in enumerate(scores):
        index = start + offset
        symptom = (
            None
            if score <= 0.0
            else _symptom(
                index, score, kind=target.kind, service=target.service, signal=target.signal
            )
        )
        transition = machine.observe(
            key=target, tick_ts=_TS + timedelta(seconds=index), symptom=symptom
        )
        outcomes.append((transition.action, transition.episode))
    return outcomes


def test_full_lifecycle_opens_sustains_clears_and_reopens_distinctly() -> None:
    machine = _machine()
    outcomes = _run(machine, [0.9, 0.9, 0.9, 0.9, 0.1, 0.1, 0.1, 0.9, 0.9, 0.9])
    actions = [action for action, _ in outcomes]

    assert actions == [
        EpisodeAction.PENDING,
        EpisodeAction.PENDING,
        EpisodeAction.OPENED,
        EpisodeAction.SUSTAINED,
        EpisodeAction.CLEARING,
        EpisodeAction.CLEARING,
        EpisodeAction.CLOSED,
        EpisodeAction.PENDING,
        EpisodeAction.PENDING,
        EpisodeAction.OPENED,
    ]

    opened = outcomes[2][1]
    assert opened is not None
    assert opened.status is EpisodeStatus.ACTIVE
    assert opened.opened_ts == _TS  # onset is the first breach, not the confirming tick
    assert opened.confirmed_ts == _TS + timedelta(seconds=2)
    assert opened.last_breach_ts == _TS + timedelta(seconds=2)
    assert opened.closed_ts is None
    assert opened.breach_tick_count == 3
    assert opened.revision == 1
    assert opened.peak_score == 0.9
    assert opened.opening_symptom_id == "symptom-0"
    assert opened.latest_symptom_id == "symptom-2"
    assert opened.evidence_refs == ("frame-0",)

    closed = outcomes[6][1]
    assert closed is not None
    assert closed.status is EpisodeStatus.CLOSED
    assert closed.episode_id == opened.episode_id
    assert closed.opened_ts == _TS
    assert closed.last_breach_ts == _TS + timedelta(seconds=3)
    assert closed.closed_ts == _TS + timedelta(seconds=6)
    assert closed.breach_tick_count == 4
    assert closed.revision == 3

    reopened = outcomes[9][1]
    assert reopened is not None
    assert reopened.episode_id != opened.episode_id  # a distinct incident
    assert reopened.opened_ts == _TS + timedelta(seconds=7)
    assert reopened.revision == 1


def test_sub_persistence_blip_never_opens() -> None:
    machine = _machine(_policy(open_after_ticks=3))
    outcomes = _run(machine, [0.9, 0.9, 0.1])

    assert [action for action, _ in outcomes] == [
        EpisodeAction.PENDING,
        EpisodeAction.PENDING,
        EpisodeAction.HELD,
    ]
    assert all(episode is None for _, episode in outcomes)
    assert machine.active_episodes() == ()


def test_deadband_scores_break_the_opening_run() -> None:
    machine = _machine(_policy(open_after_ticks=3, breach_score=0.5, clear_score=0.2))
    # Two breaches then a deadband (0.3) score: not a clear, still resets the run.
    outcomes = _run(machine, [0.9, 0.9, 0.3, 0.9, 0.9])

    assert [action for action, _ in outcomes] == [
        EpisodeAction.PENDING,
        EpisodeAction.PENDING,
        EpisodeAction.HELD,
        EpisodeAction.PENDING,
        EpisodeAction.PENDING,
    ]
    assert machine.active_episodes() == ()


def test_deadband_holds_an_open_episode_and_resets_the_clear_run() -> None:
    machine = _machine(_policy(open_after_ticks=3, close_after_ticks=3))
    # Open, take two clears, then a deadband tick that aborts the close, then clears again.
    outcomes = _run(machine, [0.9, 0.9, 0.9, 0.1, 0.1, 0.3, 0.1, 0.1])
    actions = [action for action, _ in outcomes]

    assert actions[2] is EpisodeAction.OPENED
    assert actions[3] is EpisodeAction.CLEARING
    assert actions[4] is EpisodeAction.CLEARING
    assert actions[5] is EpisodeAction.HELD  # deadband breaks the clear run
    assert actions[6] is EpisodeAction.CLEARING
    assert actions[7] is EpisodeAction.CLEARING  # count restarted, so still not closed
    assert len(machine.active_episodes()) == 1  # never closed


def test_alternating_breach_and_clear_never_closes_an_open_episode() -> None:
    machine = _machine(_policy(open_after_ticks=3, close_after_ticks=3))
    outcomes = _run(machine, [0.9, 0.9, 0.9] + [0.1, 0.9] * 6)
    actions = [action for action, _ in outcomes]

    assert actions[2] is EpisodeAction.OPENED
    assert EpisodeAction.CLOSED not in actions  # a clear run never reaches three
    assert len(machine.active_episodes()) == 1


def test_absent_symptom_is_a_clear() -> None:
    machine = _machine(_policy(open_after_ticks=2, close_after_ticks=2))
    outcomes = _run(machine, [0.9, 0.9, 0.0, 0.0])
    actions = [action for action, _ in outcomes]

    assert actions == [
        EpisodeAction.PENDING,
        EpisodeAction.OPENED,
        EpisodeAction.CLEARING,
        EpisodeAction.CLOSED,
    ]


def test_unconfigured_symptom_kind_is_ignored_without_state() -> None:
    machine = _machine()
    key = _key(SymptomKind.LOG_BURST)  # only RESIDUAL_EXCEED is configured
    transition = machine.observe(
        key=key, tick_ts=_TS, symptom=_symptom(0, 0.9, kind=SymptomKind.LOG_BURST)
    )

    assert transition.action is EpisodeAction.IGNORED
    assert transition.phase is EpisodePhase.IDLE
    assert transition.episode is None
    assert machine.active_episodes() == ()


def test_symptom_that_mismatches_the_key_fails_closed() -> None:
    machine = _machine()
    with pytest.raises(ValueError, match="does not belong"):
        machine.observe(key=_key(), tick_ts=_TS, symptom=_symptom(0, 0.9, kind=SymptomKind.DROP))


def test_out_of_order_and_conflicting_ticks_fail_closed() -> None:
    machine = _machine()
    machine.observe(key=_key(), tick_ts=_TS + timedelta(seconds=5), symptom=_symptom(5, 0.9))

    with pytest.raises(ValueError, match="event-time order"):
        machine.observe(key=_key(), tick_ts=_TS + timedelta(seconds=4), symptom=_symptom(4, 0.9))

    with pytest.raises(ValueError, match="conflicting episode tick"):
        machine.observe(key=_key(), tick_ts=_TS + timedelta(seconds=5), symptom=_symptom(99, 0.4))


def test_exact_retry_is_idempotent_and_does_not_advance_state() -> None:
    baseline = _machine()
    retried = _machine()

    scores = [0.9, 0.9, 0.9]
    baseline_outcomes = _run(baseline, scores)

    # Feed the retried machine the same ticks, but redeliver each tick once.
    retried_actions: list[EpisodeAction] = []
    for index, score in enumerate(scores):
        symptom = _symptom(index, score)
        first = retried.observe(key=_key(), tick_ts=_TS + timedelta(seconds=index), symptom=symptom)
        again = retried.observe(key=_key(), tick_ts=_TS + timedelta(seconds=index), symptom=symptom)
        assert again == first  # identical transition object, no advance
        retried_actions.append(first.action)

    assert retried_actions == [action for action, _ in baseline_outcomes]
    assert retried.active_episodes() == baseline.active_episodes()


def test_naive_timestamps_are_rejected() -> None:
    machine = _machine()
    with pytest.raises(ValueError, match="UTC"):
        machine.observe(key=_key(), tick_ts=datetime(2026, 7, 22, 12, 0), symptom=_symptom(0, 0.9))


def test_active_episodes_are_reported_sorted_by_id() -> None:
    machine = SymptomEpisodeMachine(
        configuration=EpisodeConfig(
            policies={
                SymptomKind.RESIDUAL_EXCEED.value: _policy(open_after_ticks=2),
                SymptomKind.DROP.value: _policy(open_after_ticks=2),
            }
        )
    )
    _run(
        machine, [0.9, 0.9], key=EpisodeKey(SymptomKind.RESIDUAL_EXCEED, "frontend", "request_rate")
    )
    _run(machine, [0.9, 0.9], key=EpisodeKey(SymptomKind.DROP, "checkout", "error_ratio"))

    active = machine.active_episodes()
    assert len(active) == 2
    assert [episode.episode_id for episode in active] == sorted(
        episode.episode_id for episode in active
    )


def test_open_episode_survives_json_round_trip() -> None:
    machine = _machine()
    _run(machine, [0.9, 0.7, 0.9])
    episode = machine.active_episodes()[0]

    assert SymptomEpisode.model_validate_json(episode.model_dump_json()) == episode


_SCORES = st.lists(st.sampled_from([0.0, 0.1, 0.3, 0.5, 0.9]), max_size=48)


@given(scores=_SCORES)
def test_property_episodes_never_flap(scores: list[float]) -> None:
    open_after, close_after = 3, 4
    machine = _machine(_policy(open_after_ticks=open_after, close_after_ticks=close_after))
    outcomes = _run(machine, scores)

    transitions = [
        (index, action)
        for index, (action, _) in enumerate(outcomes)
        if action in (EpisodeAction.OPENED, EpisodeAction.CLOSED)
    ]

    expected = EpisodeAction.OPENED
    previous_index: int | None = None
    for index, action in transitions:
        assert action is expected  # OPENED and CLOSED strictly alternate, OPENED first
        if previous_index is not None:
            gap = index - previous_index
            required = close_after if action is EpisodeAction.CLOSED else open_after
            assert gap >= required  # persistence bound holds in both directions
        expected = EpisodeAction.CLOSED if action is EpisodeAction.OPENED else EpisodeAction.OPENED
        previous_index = index

    if transitions and transitions[0][1] is EpisodeAction.OPENED:
        assert transitions[0][0] >= open_after - 1  # even the first open needs persistence


@given(scores=_SCORES)
def test_property_replay_is_deterministic(scores: list[float]) -> None:
    policy = _policy(open_after_ticks=3, close_after_ticks=4)
    first = [
        (action, None if episode is None else episode.model_dump(mode="json"))
        for action, episode in _run(_machine(policy), scores)
    ]
    second = [
        (action, None if episode is None else episode.model_dump(mode="json"))
        for action, episode in _run(_machine(policy), scores)
    ]

    assert first == second
