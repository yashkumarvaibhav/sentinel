"""Scenario profiles compile deterministically without mixing in answer keys."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from lab.scenarios import SeedPurpose, compile_profile, load_profile, write_artifacts
from lab.scenarios.models import ScenarioProfile

SCENARIO_ROOT = Path(__file__).resolve().parents[2] / "lab" / "scenarios"


@pytest.mark.parametrize("profile_name", ["quiet_day", "match_night", "attack_day"])
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
