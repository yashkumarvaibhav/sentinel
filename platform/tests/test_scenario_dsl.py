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
from lab.scenarios.capabilities import (
    missing_positive_kinds,
    validate_symptom_label_capabilities,
)
from lab.scenarios.models import ScenarioProfile

from contracts import SymptomKind

SCENARIO_ROOT = Path(__file__).resolve().parents[2] / "lab" / "scenarios"


@pytest.mark.parametrize(
    "profile_name",
    ["quiet_day", "match_night", "attack_day", "cascade_night", "combo_night"],
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
    assert [stimulus.kind for stimulus in profile.stimuli] == [
        "k6_journey",
        "flagd",
    ]
    edge_journey, flag = profile.stimuli
    # The checkout journey opens well before the fault so the context-blind edge
    # baseline is established on healthy calls, then spans the fault and recovery.
    assert edge_journey.start_offset_seconds == 0
    assert edge_journey.start_offset_seconds < flag.start_offset_seconds
    assert edge_journey.start_offset_seconds + edge_journey.duration_seconds == 134
    assert flag.start_offset_seconds == 84
    assert flag.start_offset_seconds + flag.duration_seconds == 104
    # The single journey fully covers the labeled fault interval (84-104 s).
    assert (
        edge_journey.start_offset_seconds
        <= flag.start_offset_seconds
        < flag.start_offset_seconds + flag.duration_seconds
        <= edge_journey.start_offset_seconds + edge_journey.duration_seconds
    )
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


def test_combo_night_compiles_attack_and_fault_without_claiming_unsupported_kinds() -> None:
    profile = load_profile(SCENARIO_ROOT / "combo_night.yml")
    artifacts = compile_profile(
        profile,
        seed=profile.seeds.development[0],
        purpose=SeedPurpose.DEVELOPMENT,
    )

    assert len(profile.contexts) == 1
    assert profile.contexts[0].expected_delta == {"frontend.request_rate": 2.5}
    assert profile.duration_seconds == 1004
    assert (
        profile.contexts[0].start_offset_seconds + profile.contexts[0].duration_seconds
        == profile.duration_seconds
    )
    assert [stimulus.kind for stimulus in profile.stimuli] == [
        "k6_journey",
        "k6_path_attack",
        "flagd",
        "k6_journey",
        "k6_journey",
        "flagd",
        "k6_rate_phase",
        "chaos_mesh",
    ]
    stimuli = {stimulus.stimulus_id: stimulus for stimulus in profile.stimuli}
    assert (
        stimuli["checkout_edge_warmup"].start_offset_seconds,
        stimuli["checkout_edge_warmup"].duration_seconds,
    ) == (0, 124)
    assert (
        stimuli["behavior_attack"].start_offset_seconds,
        stimuli["behavior_attack"].duration_seconds,
    ) == (124, 180)
    assert (
        stimuli["payment_failure"].start_offset_seconds,
        stimuli["payment_failure"].duration_seconds,
    ) == (304, 180)
    assert (
        stimuli["checkout_payment_traffic"].start_offset_seconds,
        stimuli["checkout_payment_traffic"].duration_seconds,
    ) == (304, 180)
    assert (
        stimuli["email_memory_leak"].start_offset_seconds,
        stimuli["email_memory_leak"].duration_seconds,
    ) == (484, 180)
    assert {label.kind for label in profile.symptom_labels} == {
        SymptomKind.RATIO_DEFORM.value,
        SymptomKind.LOG_BURST.value,
        SymptomKind.EDGE_DEGRADED.value,
        SymptomKind.SATURATION.value,
        SymptomKind.DROP.value,
        SymptomKind.SILENCE.value,
    }
    assert profile.residual_labels[0].stimulus_id == "behavior_attack"
    assert missing_positive_kinds(profile) == ()
    validate_symptom_label_capabilities(profile)
    assert "symptom_intervals" not in schedule_payload(artifacts.schedule)
    assert "intervals" not in schedule_payload(artifacts.schedule)


def test_combo_night_holds_explained_volume_through_attack_spin_up() -> None:
    """The phase handoff must not create a real, unlabeled traffic drop.

    Accepted captures measure 14-19 seconds between a stimulus job's requested
    start and its first delivered traffic. Every load phase overlapping that
    spin-up window after the attack's requested start must keep the full
    explained event rate flowing, otherwise the capture records a genuine
    volume collapse that no private label explains.
    """
    profile = load_profile(SCENARIO_ROOT / "combo_night.yml")
    attack = next(stimulus for stimulus in profile.stimuli if stimulus.kind == "k6_path_attack")
    spin_up_guard_seconds = 38
    explained_rate = max(phase.rate_rps for phase in profile.load_phases)
    window_start = attack.start_offset_seconds
    window_end = attack.start_offset_seconds + spin_up_guard_seconds
    offset = 0
    overlapping = []
    for phase in profile.load_phases:
        phase_start, phase_end = offset, offset + phase.duration_seconds
        if phase_start < window_end and phase_end > window_start:
            overlapping.append(phase)
        offset = phase_end
    assert overlapping
    assert all(phase.rate_rps >= explained_rate for phase in overlapping)


def test_combo_labels_follow_measured_attack_and_fault_execution() -> None:
    profile = load_profile(SCENARIO_ROOT / "combo_night.yml")
    artifacts = compile_profile(
        profile,
        seed=profile.seeds.development[0],
        purpose=SeedPurpose.DEVELOPMENT,
    )

    materialized = materialize_symptom_labels(
        artifacts,
        stimulus_offsets={
            "behavior_attack": (126.25, 306.75),
            "payment_failure": (305.5, 485.5),
            "email_memory_leak": (487.0, 667.25),
            "measured_rate_drop": (667.25, 727.5),
            "frontend_emitter_silence": (727.5, 947.75),
        },
    )

    assert materialized["intervals"] == [
        {
            "end_offset_seconds": 306.75,
            "label_id": "behavior-attack-volume",
            "start_offset_seconds": 126.25,
            "stimulus_id": "behavior_attack",
        }
    ]
    symptom_intervals = cast(list[dict[str, object]], materialized["symptom_intervals"])
    assert {
        (item["kind"], item["service"], item["signal"], item["start_offset_seconds"])
        for item in symptom_intervals
    } == {
        ("RATIO_DEFORM", "frontend", "path_entropy", 126.25),
        ("LOG_BURST", "payment", "log_template_rate", 305.5),
        ("EDGE_DEGRADED", "checkout", "dependency.payment", 305.5),
        ("SATURATION", "email", "container_memory", 487.0),
        ("DROP", "frontend", "request_rate", 667.25),
        ("SILENCE", "frontend", "request_rate", 727.5),
    }


def test_capability_oracle_rejects_unrelated_labels_and_missing_support_traffic() -> None:
    profile = load_profile(SCENARIO_ROOT / "cascade_night.yml")
    document = profile.model_dump(mode="json")
    document["symptom_labels"][0]["kind"] = "SATURATION"
    document["symptom_labels"][0]["service"] = "checkout"
    document["symptom_labels"][0]["signal"] = "container_memory"
    with pytest.raises(ValueError, match="does not prove SATURATION"):
        compile_profile(
            ScenarioProfile.model_validate(document),
            seed=profile.seeds.development[0],
            purpose=SeedPurpose.DEVELOPMENT,
        )

    document = profile.model_dump(mode="json")
    document["stimuli"] = [item for item in document["stimuli"] if item["kind"] != "k6_journey"]
    with pytest.raises(ValueError, match="overlapping checkout journey"):
        compile_profile(
            ScenarioProfile.model_validate(document),
            seed=profile.seeds.development[0],
            purpose=SeedPurpose.DEVELOPMENT,
        )

    combo = load_profile(SCENARIO_ROOT / "combo_night.yml")
    document = combo.model_dump(mode="json")
    next(item for item in document["stimuli"] if item["stimulus_id"] == "measured_rate_drop")[
        "rate_rps"
    ] = 3
    with pytest.raises(ValueError, match="must exactly match one load phase"):
        ScenarioProfile.model_validate(document)


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
            "stimulus_id": "path-attack",
            "kind": "k6_path_attack",
            "start_offset_seconds": 64,
            "duration_seconds": 20,
            "attack": "single_path",
            "path": "/",
            "rate_rps": 10,
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
            "label_id": "path-deformation",
            "kind": "RATIO_DEFORM",
            "service": "frontend",
            "signal": "path_entropy",
            "stimulus_id": "path-attack",
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
        "k6_path_attack",
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
