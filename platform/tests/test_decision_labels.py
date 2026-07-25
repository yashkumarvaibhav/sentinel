"""The decision-level answer key: what each scenario ought to conclude."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any, get_args

import pytest
import yaml
from lab.scenarios import load_profile
from lab.scenarios.models import NAMED_OUTCOMES, LabeledOutcome, ScenarioProfile, ScoredSymptomKind
from pydantic import ValidationError

from common.config import load_config
from contracts import SymptomKind, VerdictClass

REPO_ROOT = Path(__file__).resolve().parents[2]
SCENARIO_ROOT = REPO_ROOT / "lab" / "scenarios"
CONFIG_ROOT = REPO_ROOT / "config"
PROFILE_NAMES = ("quiet_day", "match_night", "attack_day", "cascade_night", "combo_night")


def _profiles() -> dict[str, ScenarioProfile]:
    return {name: load_profile(SCENARIO_ROOT / f"{name}.yml") for name in PROFILE_NAMES}


def _document(name: str = "cascade_night") -> dict[str, Any]:
    value = yaml.safe_load((SCENARIO_ROOT / f"{name}.yml").read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return copy.deepcopy(value)


def test_the_answer_key_vocabulary_cannot_drift_from_the_contracts() -> None:
    """The lab DSL restates these enums as literals; a silent divergence is a bug."""
    labelled = set(get_args(LabeledOutcome.__value__))
    assert labelled - {"UNEXPLAINED"} == {member.value for member in VerdictClass}
    assert labelled > NAMED_OUTCOMES
    # The same guard for the symptom vocabulary, which has never had one.
    scored = set(get_args(ScoredSymptomKind.__value__))
    assert scored == {member.value for member in SymptomKind} - {SymptomKind.DEPLOY_MARKER.value}


def test_every_labelled_origin_is_a_service_the_topology_knows() -> None:
    """An answer key naming a service that does not exist would never match anything."""
    known = {service.service for service in load_config(CONFIG_ROOT).topology.services}

    named = {
        label.origin_service
        for profile in _profiles().values()
        for label in profile.decision_labels
        if label.origin_service is not None
    }

    assert named
    assert named <= known


def test_every_decision_label_is_anchored_to_evidence_that_exists() -> None:
    for name, profile in _profiles().items():
        evidence = {label.label_id for label in profile.residual_labels}
        evidence |= {label.label_id for label in profile.symptom_labels}
        for label in profile.decision_labels:
            assert set(label.label_refs) <= evidence, f"{name}/{label.label_id}"
            assert label.label_refs, f"{name}/{label.label_id}"


def test_the_negative_control_expects_nothing_at_all() -> None:
    assert _profiles()["quiet_day"].decision_labels == ()


def test_the_pure_volume_profiles_expect_no_class_to_be_named() -> None:
    """Volume alone can never be called hostile - the guiding principle, as an answer key."""
    for name in ("attack_day", "match_night"):
        labels = _profiles()[name].decision_labels
        assert [label.expectation for label in labels] == ["UNEXPLAINED"]
        assert labels[0].origin_service is None


def test_the_behavioural_attack_is_the_one_labelled_attack() -> None:
    """Attack recall needs a denominator the evidence can actually support."""
    attacks = {
        name: [label for label in profile.decision_labels if label.expectation == "ATTACK"]
        for name, profile in _profiles().items()
    }

    assert [name for name, labels in attacks.items() if labels] == ["combo_night"]
    assert attacks["combo_night"][0].origin_service == "frontend"
    # It is labelled inside the legitimate match window: an attack hiding in an event.
    combo = _profiles()["combo_night"]
    assert combo.contexts and combo.contexts[0].context_id == "simulated_match"


def test_a_named_class_must_say_where_it_started() -> None:
    document = _document()
    document["decision_labels"][0].pop("origin_service")

    with pytest.raises(ValidationError, match="must name the service it started at"):
        ScenarioProfile.model_validate(document)


def test_an_outcome_that_names_no_class_must_not_name_a_culprit() -> None:
    document = _document("match_night")
    document["decision_labels"][0]["origin_service"] = "frontend"

    with pytest.raises(ValidationError, match="must not name an originating service"):
        ScenarioProfile.model_validate(document)


def test_a_decision_label_cannot_invent_its_own_evidence() -> None:
    document = _document()
    document["decision_labels"][0]["label_refs"] = ["a-label-nobody-declared"]

    with pytest.raises(ValidationError, match="references unknown evidence labels"):
        ScenarioProfile.model_validate(document)


def test_a_decision_label_may_not_reuse_an_evidence_label_id() -> None:
    document = _document()
    document["decision_labels"][0]["label_id"] = "checkout-payment-failure"

    with pytest.raises(ValidationError, match="label ids must be unique"):
        ScenarioProfile.model_validate(document)
