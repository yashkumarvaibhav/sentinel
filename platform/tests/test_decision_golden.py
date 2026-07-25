"""The decision plane's semantic regression gate and the pin it is frozen under."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from lab.scoring.decision_golden import (
    GOLDEN_PROFILES,
    check_goldens,
    load_candidates,
    write_goldens,
)
from lab.scoring.decisions import load_decision_configs

from common.config import load_config

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_ROOT = REPO_ROOT / "config"
GOLDENS_ROOT = REPO_ROOT / "lab" / "goldens" / "phase-4"
CAPTURES_ROOT = REPO_ROOT / "data" / "captures" / "phase-1" / "goldens"


def _committed(profile_name: str) -> dict[str, Any]:
    value = json.loads((GOLDENS_ROOT / f"{profile_name}.json").read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def test_the_committed_goldens_name_both_configuration_surfaces() -> None:
    """The pin: a decision depends on what produced its episodes and on what judged them."""
    config = load_config(CONFIG_ROOT)
    decisions = load_decision_configs(CONFIG_ROOT)

    for profile_name in GOLDEN_PROFILES:
        golden = _committed(profile_name)
        assert golden["detector_config_fingerprint"] == config.fingerprint
        assert golden["decision_config_fingerprint"] == decisions.fingerprint


def test_the_quiet_day_golden_freezes_a_plane_that_says_nothing() -> None:
    """The negative control, one plane up: no episodes means no decision at all."""
    golden = _committed("quiet_day")

    assert golden["episode_count"] == 0
    assert golden["ticks"] == []


def test_the_match_night_golden_freezes_an_alert_that_never_acts() -> None:
    """An unexplained residual under a legitimate event is told, never acted on."""
    golden = _committed("match_night")

    incidents = [incident for tick in golden["ticks"] for incident in tick["incidents"]]
    assert incidents
    for incident in incidents:
        decision = incident["decision"]
        assert decision["action"] == "ALERT"
        assert decision["rule_id"] == "tell-someone-what-we-cannot-name"
        assert decision["target_service"] is None
        assert decision["requires_human_approval"] is False


def test_a_changed_transcript_is_reported_as_a_readable_diff(tmp_path: Path) -> None:
    frozen = dict.fromkeys(GOLDEN_PROFILES, b'{\n  "answer": "the reviewed one"\n}\n')
    write_goldens(frozen, tmp_path)
    drifted = dict(frozen)
    drifted[GOLDEN_PROFILES[0]] = b'{\n  "answer": "something else"\n}\n'

    check_goldens(frozen, tmp_path)
    with pytest.raises(ValueError, match="decision golden changed") as error:
        check_goldens(drifted, tmp_path)
    # The point of an indented artifact: the failure shows what moved.
    assert "the reviewed one" in str(error.value)
    assert "something else" in str(error.value)


def test_a_missing_or_extra_golden_is_refused(tmp_path: Path) -> None:
    frozen = dict.fromkeys(GOLDEN_PROFILES, b"{}\n")
    write_goldens(frozen, tmp_path)

    (tmp_path / f"{GOLDEN_PROFILES[0]}.json").unlink()
    with pytest.raises(ValueError, match="must exactly match the golden profiles"):
        check_goldens(frozen, tmp_path)

    write_goldens(frozen, tmp_path)
    (tmp_path / "an_uninvited_profile.json").write_bytes(b"{}\n")
    with pytest.raises(ValueError, match="must exactly match the golden profiles"):
        check_goldens(frozen, tmp_path)


def test_the_committed_goldens_are_exactly_what_the_replay_produces() -> None:
    if not CAPTURES_ROOT.is_dir():
        pytest.skip("phase-1 golden captures are not present (run make data-pull)")

    candidates = load_candidates(repo_root=REPO_ROOT, captures_root=CAPTURES_ROOT)

    assert set(candidates) == set(GOLDEN_PROFILES)
    check_goldens(candidates, GOLDENS_ROOT)
