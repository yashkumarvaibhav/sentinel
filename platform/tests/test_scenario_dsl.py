"""Scenario profiles compile deterministically without mixing in answer keys."""

from __future__ import annotations

import json
from pathlib import Path
from typing import cast

import pytest
from lab.scenarios import (
    SeedPurpose,
    compile_profile,
    load_profile,
    materialize_symptom_labels,
    schedule_payload,
    write_artifacts,
)
from lab.scenarios.models import ScenarioProfile

SCENARIO_ROOT = Path(__file__).resolve().parents[2] / "lab" / "scenarios"


@pytest.mark.parametrize(
    "profile_name", ["quiet_day", "match_night", "attack_day", "cascade_night"]
)
def test_committed_profiles_have_disjoint_seed_sets_and_bounded_fixed_target_load(
    profile_name: str,
) -> None:
    profile = load_profile(SCENARIO_ROOT / f"{profile_name}.yml")

    assert set(profile.seeds.development).isdisjoint(profile.seeds.held_out)
    assert profile.telemetry.target == "astronomy-shop/frontend-proxy"
    assert max(phase.rate_rps for phase in profile.load_phases) <= 50


def test_attack_day_is_a_no_event_control_with_a_labeled_surge() -> None:
    profile = load_profile(SCENARIO_ROOT / "attack_day.yml")
    artifacts = compile_profile(
        profile,
        seed=profile.seeds.held_out[0],
        purpose=SeedPurpose.HELD_OUT,
    )

    # The control profile carries a labeled attack but *no* event context, so a
    # detection can never be explained away by the calendar.
    assert profile.contexts == ()
    assert artifacts.context_feed["windows"] == []
    assert profile.residual_labels
    assert artifacts.labels["intervals"]


def test_cascade_night_keeps_volume_explained_while_a_downstream_fault_runs() -> None:
    profile = load_profile(SCENARIO_ROOT / "cascade_night.yml")
    artifacts = compile_profile(
        profile,
        seed=profile.seeds.development[0],
        purpose=SeedPurpose.DEVELOPMENT,
    )

    assert profile.residual_labels == ()
    assert len(profile.contexts) == 1
    context = profile.contexts[0]
    assert context.expected_delta == {"frontend.request_rate": 2.5}
    assert context.start_offset_seconds == 64
    assert context.start_offset_seconds + context.duration_seconds == profile.duration_seconds
    assert [stimulus.kind for stimulus in profile.stimuli] == ["flagd", "k6_journey"]
    for stimulus in profile.stimuli:
        assert stimulus.start_offset_seconds == 84
        assert stimulus.start_offset_seconds + stimulus.duration_seconds == 104
    assert artifacts.labels["symptom_intervals"] == [
        {
            "end_offset_seconds": 104,
            "kind": "EDGE_DEGRADED",
            "label_id": "checkout-payment-failure",
            "service": "checkout",
            "signal": "dependency.payment",
            "start_offset_seconds": 84,
            "stimulus_id": "payment_failure",
        }
    ]
    assert "symptom_intervals" not in schedule_payload(artifacts.schedule)


def test_capture_labels_follow_measured_stimulus_execution_not_planned_offsets() -> None:
    profile = load_profile(SCENARIO_ROOT / "cascade_night.yml")
    artifacts = compile_profile(
        profile,
        seed=profile.seeds.development[0],
        purpose=SeedPurpose.DEVELOPMENT,
    )

    materialized = materialize_symptom_labels(
        artifacts,
        stimulus_offsets={"payment_failure": (87.901232, 107.929988)},
    )

    assert materialized["symptom_intervals"] == [
        {
            "end_offset_seconds": 107.929988,
            "kind": "EDGE_DEGRADED",
            "label_id": "checkout-payment-failure",
            "service": "checkout",
            "signal": "dependency.payment",
            "start_offset_seconds": 87.901232,
            "stimulus_id": "payment_failure",
        }
    ]
    with pytest.raises(ValueError, match="missing measured execution"):
        materialize_symptom_labels(artifacts, stimulus_offsets={})


def test_compilation_is_byte_stable_and_seed_purpose_is_enforced() -> None:
    profile = load_profile(SCENARIO_ROOT / "match_night.yml")
    seed = profile.seeds.held_out[0]

    first = compile_profile(profile, seed=seed, purpose=SeedPurpose.HELD_OUT)
    second = compile_profile(profile, seed=seed, purpose=SeedPurpose.HELD_OUT)

    assert first.canonical_bytes() == second.canonical_bytes()
    assert first.schedule.seed == seed
    assert first.schedule.request_mix_seed != 0
    with pytest.raises(ValueError, match="development"):
        compile_profile(profile, seed=seed, purpose=SeedPurpose.DEVELOPMENT)


def test_compiler_emits_public_schedule_context_and_private_labels_separately(
    tmp_path: Path,
) -> None:
    profile = load_profile(SCENARIO_ROOT / "match_night.yml")
    artifacts = compile_profile(
        profile,
        seed=profile.seeds.development[0],
        purpose=SeedPurpose.DEVELOPMENT,
    )

    paths = write_artifacts(artifacts, tmp_path)

    assert paths.labels.name == "labels.json"
    assert paths.labels.parent.name == "private"
    schedule = json.loads(paths.schedule.read_text(encoding="utf-8"))
    context = json.loads(paths.context_feed.read_text(encoding="utf-8"))
    labels = json.loads(paths.labels.read_text(encoding="utf-8"))
    assert "labels" not in schedule
    assert "labels" not in context
    assert labels["intervals"]
    assert context["windows"]
    assert all("expected_residual" not in phase for phase in schedule["phases"])


def test_fault_stimuli_compile_publicly_while_symptom_labels_stay_private() -> None:
    profile = load_profile(SCENARIO_ROOT / "quiet_day.yml")
    document = profile.model_dump(mode="json")
    document["stimuli"] = [
        {
            "stimulus_id": "email-leak",
            "kind": "flagd",
            "start_offset_seconds": 64,
            "duration_seconds": 20,
            "flag": "emailMemoryLeak",
            "variant": "100x",
        },
        {
            "stimulus_id": "ad-pressure",
            "kind": "chaos_mesh",
            "start_offset_seconds": 64,
            "duration_seconds": 20,
            "experiment": "ad-cpu-pressure",
        },
        {
            "stimulus_id": "checkout-traffic",
            "kind": "k6_journey",
            "start_offset_seconds": 64,
            "duration_seconds": 20,
            "journey": "checkout",
            "rate_rps": 2,
        },
    ]
    document["symptom_labels"] = [
        {
            "label_id": "email-saturation",
            "kind": "SATURATION",
            "service": "email",
            "signal": "process.runtime.jvm.memory.usage",
            "start_offset_seconds": 64,
            "end_offset_seconds": 84,
        }
    ]
    artifacts = compile_profile(
        ScenarioProfile.model_validate(document),
        seed=profile.seeds.development[0],
        purpose=SeedPurpose.DEVELOPMENT,
    )

    public_schedule = schedule_payload(artifacts.schedule)
    stimuli = cast(list[dict[str, object]], public_schedule["stimuli"])
    assert [item["kind"] for item in stimuli] == [
        "flagd",
        "chaos_mesh",
        "k6_journey",
    ]
    assert "symptom_labels" not in public_schedule
    assert "symptom_intervals" not in public_schedule
    assert artifacts.labels["symptom_intervals"] == document["symptom_labels"]


def test_fault_stimuli_are_bounded_and_same_target_overlap_is_rejected() -> None:
    profile = load_profile(SCENARIO_ROOT / "quiet_day.yml")
    document = profile.model_dump(mode="json")
    document["stimuli"] = [
        {
            "stimulus_id": "too-long",
            "kind": "flagd",
            "start_offset_seconds": 64,
            "duration_seconds": 21,
            "flag": "emailMemoryLeak",
            "variant": "100x",
        }
    ]
    with pytest.raises(ValueError, match="stimulus exceeds scenario duration"):
        ScenarioProfile.model_validate(document)

    document["stimuli"] = [
        {
            "stimulus_id": "first",
            "kind": "flagd",
            "start_offset_seconds": 60,
            "duration_seconds": 20,
            "flag": "emailMemoryLeak",
            "variant": "10x",
        },
        {
            "stimulus_id": "second",
            "kind": "flagd",
            "start_offset_seconds": 70,
            "duration_seconds": 10,
            "flag": "emailMemoryLeak",
            "variant": "100x",
        },
    ]
    with pytest.raises(ValueError, match="overlapping stimuli for flagd:emailMemoryLeak"):
        ScenarioProfile.model_validate(document)


def test_invalid_seed_overlap_and_over_cap_rate_fail_validation() -> None:
    profile = load_profile(SCENARIO_ROOT / "quiet_day.yml")
    document = profile.model_dump(mode="json")
    document["seeds"]["held_out"] = document["seeds"]["development"]
    with pytest.raises(ValueError, match="disjoint"):
        ScenarioProfile.model_validate(document)

    document = profile.model_dump(mode="json")
    document["load_phases"][0]["rate_rps"] = 51
    with pytest.raises(ValueError, match="less than or equal to 50"):
        ScenarioProfile.model_validate(document)


def test_duplicate_scenario_keys_are_rejected(tmp_path: Path) -> None:
    path = tmp_path / "duplicate.yml"
    path.write_text("version: 1\nversion: 1\n", encoding="utf-8")

    with pytest.raises(ValueError, match="duplicate key"):
        load_profile(path)
