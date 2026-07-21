"""Cross-plane contracts reject ambiguous or unsafe telemetry at the boundary."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from math import isclose

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import BaseModel, ValidationError

from contracts import ContextWindow, DecompFrame, Observation, Symptom, SymptomKind

UTC_TS = datetime(2026, 7, 21, 12, 0, tzinfo=UTC)


@pytest.fixture
def observation() -> Observation:
    return Observation(
        observation_id="obs-frontend-rps-001",
        ts=UTC_TS,
        service="frontend",
        signal="request_rate",
        value=412.5,
        unit="requests/s",
        attributes={"http.request.method": "GET", "http.response.status_code": 200},
        flow_refs=("flow-001",),
        log_refs=("log-001",),
        trace_refs=("4bf92f3577b34da6a3ce929d0e0e4736",),
    )


@pytest.fixture
def context_window() -> ContextWindow:
    return ContextWindow(
        context_id="event-match-001",
        name="Continental cup final",
        event_type="sports_fixture",
        source="fixtures-api",
        honesty="REAL",
        valid_from=UTC_TS,
        valid_to=UTC_TS + timedelta(hours=3),
        expected_delta={"frontend.request_rate": 3.0, "checkout.request_rate": 1.5},
        trust_score=0.95,
    )


@pytest.fixture
def decomp_frame() -> DecompFrame:
    return DecompFrame(
        frame_id="frame-001",
        observation_id="obs-frontend-rps-001",
        ts=UTC_TS,
        service="frontend",
        signal="request_rate",
        observed=412.5,
        explained_base=100.0,
        explained_event=300.0,
        residual=12.5,
        band_low=380.0,
        band_high=420.0,
        residual_score=0.2,
        context_ids=("event-match-001",),
    )


@pytest.fixture
def symptom() -> Symptom:
    return Symptom(
        symptom_id="symptom-001",
        kind=SymptomKind.RESIDUAL_EXCEED,
        service="frontend",
        signal="request_rate",
        onset_ts=UTC_TS,
        score=0.84,
        note="Residual exceeded the context-aware upper band.",
        evidence_refs=("frame-001",),
    )


@pytest.mark.parametrize(
    "fixture_name", ["observation", "context_window", "decomp_frame", "symptom"]
)
def test_contract_json_round_trip(request: pytest.FixtureRequest, fixture_name: str) -> None:
    contract = request.getfixturevalue(fixture_name)

    restored = type(contract).model_validate_json(contract.model_dump_json())

    assert restored == contract


def test_observation_has_no_ground_truth_fields_and_rejects_unknown_fields(
    observation: Observation,
) -> None:
    forbidden = {"label", "scenario_label", "attack_flag", "injected_fault_id", "ground_truth"}

    assert forbidden.isdisjoint(Observation.model_fields)
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        Observation.model_validate({**observation.model_dump(), "attack_flag": True})


@pytest.mark.parametrize(
    "ground_truth_key",
    ["ground_truth", "scenario.label", "attack_flag", "injected-fault-id", "expected_verdict"],
)
def test_observation_rejects_ground_truth_hidden_in_attributes(
    observation: Observation, ground_truth_key: str
) -> None:
    payload = observation.model_dump()
    payload["attributes"] = {ground_truth_key: "attack"}

    with pytest.raises(ValidationError, match="ground-truth"):
        Observation.model_validate(payload)


@pytest.mark.parametrize("field", ["ts", "valid_from", "valid_to", "onset_ts"])
def test_contract_timestamps_must_be_utc(field: str) -> None:
    local_ts = datetime(2026, 7, 21, 13, 0, tzinfo=timezone(timedelta(hours=1)))
    cases: dict[str, tuple[type[BaseModel], dict[str, object]]] = {
        "ts": (
            Observation,
            {
                "observation_id": "obs-1",
                "ts": local_ts,
                "service": "frontend",
                "signal": "request_rate",
                "value": 1.0,
            },
        ),
        "valid_from": (
            ContextWindow,
            {
                "context_id": "ctx-1",
                "name": "Match",
                "event_type": "sports_fixture",
                "source": "calendar",
                "valid_from": local_ts,
                "valid_to": UTC_TS + timedelta(hours=2),
                "expected_delta": {"request_rate": 1.0},
                "trust_score": 1.0,
            },
        ),
        "valid_to": (
            ContextWindow,
            {
                "context_id": "ctx-1",
                "name": "Match",
                "event_type": "sports_fixture",
                "source": "calendar",
                "valid_from": UTC_TS,
                "valid_to": local_ts + timedelta(hours=2),
                "expected_delta": {"request_rate": 1.0},
                "trust_score": 1.0,
            },
        ),
        "onset_ts": (
            Symptom,
            {
                "symptom_id": "symptom-1",
                "kind": SymptomKind.SILENCE,
                "service": "frontend",
                "signal": "request_rate",
                "onset_ts": local_ts,
                "score": 1.0,
                "note": "Emitter stopped reporting.",
            },
        ),
    }
    contract, payload = cases[field]

    with pytest.raises(ValidationError, match="UTC"):
        contract.model_validate(payload)


def test_observation_rejects_empty_identifiers_non_finite_values_and_duplicate_refs(
    observation: Observation,
) -> None:
    updates: tuple[dict[str, object], ...] = (
        {"service": "   "},
        {"value": float("nan")},
        {"attributes": {"latency": float("inf")}},
        {"trace_refs": ("trace-1", "trace-1")},
    )
    for update in updates:
        with pytest.raises(ValidationError):
            Observation.model_validate({**observation.model_dump(), **update})


def test_context_window_requires_a_forward_range_expected_signals_and_bounded_trust(
    context_window: ContextWindow,
) -> None:
    updates: tuple[dict[str, object], ...] = (
        {"valid_to": context_window.valid_from},
        {"expected_delta": {}},
        {"trust_score": 1.01},
    )
    for update in updates:
        with pytest.raises(ValidationError):
            ContextWindow.model_validate({**context_window.model_dump(), **update})


def test_decomp_frame_rejects_broken_arithmetic_and_invalid_band(
    decomp_frame: DecompFrame,
) -> None:
    with pytest.raises(ValidationError, match="decomposition invariant"):
        DecompFrame.model_validate({**decomp_frame.model_dump(), "residual": 99.0})

    with pytest.raises(ValidationError, match="band_low"):
        DecompFrame.model_validate(
            {**decomp_frame.model_dump(), "band_low": 500.0, "band_high": 400.0}
        )


def test_symptom_rejects_unknown_kind_empty_note_and_out_of_range_score(symptom: Symptom) -> None:
    updates: tuple[dict[str, object], ...] = (
        {"kind": "THRESHOLD_BREACH"},
        {"note": "   "},
        {"score": -0.01},
    )
    for update in updates:
        with pytest.raises(ValidationError):
            Symptom.model_validate({**symptom.model_dump(), **update})


finite_component = st.floats(
    min_value=-1e9,
    max_value=1e9,
    allow_nan=False,
    allow_infinity=False,
    width=64,
)


@given(base=finite_component, event=finite_component, residual=finite_component)
def test_decomposition_invariant_accepts_consistent_finite_components(
    base: float, event: float, residual: float
) -> None:
    observed = base + event + residual
    frame = DecompFrame(
        frame_id="frame-property",
        observation_id="obs-property",
        ts=UTC_TS,
        service="service",
        signal="signal",
        observed=observed,
        explained_base=base,
        explained_event=event,
        residual=residual,
        band_low=observed - 1.0,
        band_high=observed + 1.0,
        residual_score=0.5,
    )

    assert isclose(
        frame.observed,
        frame.explained_base + frame.explained_event + frame.residual,
        rel_tol=1e-9,
        abs_tol=1e-9,
    )
