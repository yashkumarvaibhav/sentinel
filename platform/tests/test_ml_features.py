"""The aware/blind feature schema and its encodings."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from ml.features import (
    AWARE_FEATURES,
    EVENT_FEATURES,
    TEMPORAL_FEATURES,
    ActiveEvent,
    aware_features,
    context_blind_view,
    event_features,
    temporal_features,
)

# 2026-01-05 is a Monday (weekday 0); 2026-01-10 is the following Saturday.
_MONDAY_MIDNIGHT = datetime(2026, 1, 5, 0, 0, tzinfo=UTC)
_MONDAY_0600 = datetime(2026, 1, 5, 6, 0, tzinfo=UTC)
_SATURDAY_NOON = datetime(2026, 1, 10, 12, 0, tzinfo=UTC)


def test_temporal_features_at_monday_midnight() -> None:
    features = temporal_features(_MONDAY_MIDNIGHT)
    assert features["tod_sin"] == pytest.approx(0.0, abs=1e-12)
    assert features["tod_cos"] == pytest.approx(1.0)
    assert features["dow_sin"] == pytest.approx(0.0, abs=1e-12)
    assert features["dow_cos"] == pytest.approx(1.0)
    assert features["is_weekend"] == 0.0


def test_time_of_day_is_cyclic_at_six_hours() -> None:
    features = temporal_features(_MONDAY_0600)
    assert features["tod_sin"] == pytest.approx(1.0)
    assert features["tod_cos"] == pytest.approx(0.0, abs=1e-12)


def test_weekend_is_flagged() -> None:
    assert temporal_features(_SATURDAY_NOON)["is_weekend"] == 1.0


def test_event_features_match_decomposition_lift() -> None:
    active = (
        ActiveEvent(multiplier=2.5, trust_score=0.9),
        ActiveEvent(multiplier=1.8, trust_score=0.75),
    )
    features = event_features(active)
    # sum of max(mult - 1, 0) * trust = 1.5*0.9 + 0.8*0.75
    assert features["event_lift"] == pytest.approx(1.35 + 0.6)
    assert features["event_count"] == 2.0
    assert features["event_trust_max"] == pytest.approx(0.9)


def test_event_features_with_no_active_events() -> None:
    features = event_features(())
    assert features == {"event_lift": 0.0, "event_count": 0.0, "event_trust_max": 0.0}


def test_a_below_one_multiplier_contributes_no_lift() -> None:
    features = event_features((ActiveEvent(multiplier=0.5, trust_score=1.0),))
    assert features["event_lift"] == 0.0


def test_aware_and_blind_schemas() -> None:
    features = aware_features(_SATURDAY_NOON, (ActiveEvent(multiplier=2.0, trust_score=0.5),))
    assert set(features) == set(AWARE_FEATURES)
    blind = context_blind_view(features)
    assert set(blind) == set(TEMPORAL_FEATURES)
    assert not set(blind) & set(EVENT_FEATURES)
