"""Live scoring keeps workload targets and extraction queries contained."""

from __future__ import annotations

import json
import math
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import lab.scoring.live as live_module
import pytest
from lab.scenarios import SeedPurpose, compile_profile, load_profile
from lab.scenarios.compiler import CompiledSchedule
from lab.scenarios.models import ScenarioProfile
from lab.scoring.live import (
    build_checkout_journey_job,
    build_k6_job,
    build_path_attack_job,
    execute_stimuli,
    expected_request_count,
    render_stimulus_executions,
)

SCENARIO_ROOT = Path(__file__).resolve().parents[2] / "lab" / "scenarios"


def test_live_job_is_resource_capped_in_namespace_with_no_target_override() -> None:
    profile = load_profile(SCENARIO_ROOT / "match_night.yml")
    artifacts = compile_profile(
        profile,
        seed=profile.seeds.development[0],
        purpose=SeedPurpose.DEVELOPMENT,
    )

    job = build_k6_job(artifacts.schedule, job_name="score-match-211", run_id="run-abc")
    container = job["spec"]["template"]["spec"]["containers"][0]

    assert job["metadata"]["namespace"] == "otel-demo"
    assert container["image"] == "grafana/k6:0.55.0"
    assert container["resources"]["limits"] == {"cpu": "1", "memory": "256Mi"}
    env = {item["name"]: item["value"] for item in container["env"]}
    assert "TARGET" not in env
    assert env["SENTINEL_RUN_ID"] == "run-abc"
    assert expected_request_count(artifacts.schedule) == 712


def test_checkout_journey_is_rate_capped_and_has_no_target_override() -> None:
    schedule = _fault_schedule(flag_variant="100x", include_journey=True)
    stimulus = next(item for item in schedule.stimuli if item.kind == "k6_journey")

    job = build_checkout_journey_job(
        stimulus,
        job_name="stim-checkout",
        run_id="journey-abc",
    )
    container = job["spec"]["template"]["spec"]["containers"][0]
    env = {item["name"]: item["value"] for item in container["env"]}

    assert job["metadata"]["namespace"] == "otel-demo"
    assert container["image"] == "grafana/k6:0.55.0"
    assert container["resources"]["limits"] == {"cpu": "1", "memory": "256Mi"}
    assert "TARGET" not in env
    assert env["SENTINEL_JOURNEY"] == "checkout"
    assert env["SENTINEL_RATE_RPS"] == "2"
    assert env["SENTINEL_DURATION_SECONDS"] == "20"


def test_combo_path_attack_is_fixed_target_capped_and_counted_as_primary_volume() -> None:
    profile = load_profile(SCENARIO_ROOT / "combo_night.yml")
    schedule = compile_profile(
        profile,
        seed=profile.seeds.development[0],
        purpose=SeedPurpose.DEVELOPMENT,
    ).schedule
    stimulus = next(item for item in schedule.stimuli if item.kind == "k6_path_attack")

    job = build_path_attack_job(
        stimulus,
        job_name="stim-path-attack",
        run_id="attack-abc",
        user_agent="sentinel-score/run-combo/attack/abc",
    )
    container = job["spec"]["template"]["spec"]["containers"][0]
    env = {item["name"]: item["value"] for item in container["env"]}

    assert container["resources"]["limits"] == {"cpu": "1", "memory": "256Mi"}
    assert "TARGET" not in env
    assert env["SENTINEL_JOURNEY"] == "path_attack"
    assert env["SENTINEL_ATTACK_PATH"] == "/"
    assert env["SENTINEL_RATE_RPS"] == "20"
    assert env["SENTINEL_USER_AGENT"] == "sentinel-score/run-combo/attack/abc"
    assert expected_request_count(schedule) == 12_176


def test_primary_rate_phase_records_measured_boundaries_without_a_second_job() -> None:
    profile = load_profile(SCENARIO_ROOT / "combo_night.yml")
    schedule = compile_profile(
        profile,
        seed=profile.seeds.development[0],
        purpose=SeedPurpose.DEVELOPMENT,
    ).schedule
    rate_phase = next(item for item in schedule.stimuli if item.kind == "k6_rate_phase")
    minimal = replace(schedule, stimuli=(rate_phase,))
    commands: list[list[str]] = []
    anchor = datetime(2026, 7, 22, 12, 0, tzinfo=UTC)
    times = iter(
        (
            anchor + timedelta(seconds=484, milliseconds=10),
            anchor + timedelta(seconds=544, milliseconds=20),
        )
    )

    def runner(
        command: list[str],
        *,
        input_text: str | None = None,
        timeout: int = 120,
    ) -> str:
        del input_text, timeout
        commands.append(command)
        return ""

    executions = execute_stimuli(
        repo_root=SCENARIO_ROOT.parents[1],
        schedule=minimal,
        anchor_ts=anchor,
        runner=runner,
        waiter=lambda _: None,
        clock=lambda: next(times),
    )

    assert commands == []
    assert executions[0].kind == "k6_rate_phase"
    assert executions[0].setting == "primary@2rps"
    assert executions[0].started_at == anchor + timedelta(seconds=484, milliseconds=10)
    assert executions[0].ended_at == anchor + timedelta(seconds=544, milliseconds=20)


def test_live_anchor_waits_for_the_complete_marker_burst(monkeypatch: pytest.MonkeyPatch) -> None:
    start = datetime(2026, 7, 22, 12, 0, tzinfo=UTC)
    responses = [
        (start,),
        tuple(start + timedelta(milliseconds=index) for index in range(10)),
    ]

    def query_spans(*, repo_root: Path, user_agent: str) -> tuple[datetime, ...]:
        del repo_root, user_agent
        return responses.pop(0)

    monkeypatch.setattr(live_module, "_query_spans", query_spans)
    monkeypatch.setattr("lab.scoring.live.time.sleep", lambda _: None)

    anchor = live_module._wait_for_anchor(
        repo_root=SCENARIO_ROOT.parents[1],
        user_agent="sentinel-score-anchor/run-test",
    )

    assert anchor == start + timedelta(milliseconds=9)
    assert responses == []


def test_live_anchor_accepts_a_stable_partial_marker_burst(monkeypatch: pytest.MonkeyPatch) -> None:
    start = datetime(2026, 7, 22, 12, 0, tzinfo=UTC)
    partial = tuple(start + timedelta(milliseconds=index) for index in range(4))
    responses = [partial, partial, partial, partial]

    def query_spans(*, repo_root: Path, user_agent: str) -> tuple[datetime, ...]:
        del repo_root, user_agent
        return responses.pop(0)

    monkeypatch.setattr(live_module, "_query_spans", query_spans)
    monkeypatch.setattr("lab.scoring.live.time.sleep", lambda _: None)

    anchor = live_module._wait_for_anchor(
        repo_root=SCENARIO_ROOT.parents[1],
        user_agent="sentinel-score-anchor/run-test",
    )

    assert anchor == start + timedelta(milliseconds=3)
    assert responses == []


def test_span_query_deduplicates_by_observation_without_unbounded_final(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands: list[list[str]] = []

    def run(command: list[str], **_: object) -> str:
        commands.append(command)
        return (
            '{"observation_id":"one","ts_us":"1784721600000000"}\n'
            '{"observation_id":"two","ts_us":"1784721601000000"}\n'
        )

    monkeypatch.setattr(live_module, "_run", run)

    timestamps = live_module._query_spans(
        repo_root=SCENARIO_ROOT.parents[1],
        user_agent="sentinel-stimulus/journey-bounded",
    )

    query = commands[0][-1]
    assert " FINAL" not in query
    assert "PREWHERE service = 'frontend-proxy'" in query
    assert "startsWith" in query
    assert "GROUP BY observation_id" in query
    assert timestamps[1] - timestamps[0] == timedelta(seconds=1)


def test_live_stimuli_use_fixed_owned_resources_and_restore_flagd() -> None:
    schedule = _fault_schedule(flag_variant="100x", include_journey=True)
    commands: list[list[str]] = []
    waits: list[datetime] = []
    span_reads: list[tuple[str, int]] = []
    events: list[str] = []
    anchor = datetime(2026, 7, 22, 12, 0, tzinfo=UTC)

    def runner(command: list[str], *, input_text: str | None = None, timeout: int = 120) -> str:
        del input_text, timeout
        commands.append(command)
        events.append("command")
        if "get" in command and "configmap" in command:
            return _flag_config_map()
        if "get" in command and "job" in command:
            return '{"status":{"succeeded":1}}'
        if "logs" in command:
            return "iterations dropped_iterations"
        return ""

    def read_spans(user_agent: str, expected: int) -> tuple[datetime, ...]:
        span_reads.append((user_agent, expected))
        events.append("read-spans")
        return tuple(
            anchor + timedelta(seconds=66, milliseconds=index)
            for index in range(math.ceil(expected * 0.95))
        )

    executions = execute_stimuli(
        repo_root=SCENARIO_ROOT.parents[1],
        schedule=schedule,
        anchor_ts=anchor,
        runner=runner,
        waiter=waits.append,
        clock=lambda: anchor + timedelta(seconds=65),
        journey_span_reader=read_spans,
    )

    assert waits == [anchor + timedelta(seconds=64), anchor + timedelta(seconds=84)]
    assert {item.stimulus_id for item in executions} == {
        "ad-pressure",
        "checkout-traffic",
        "email-leak",
    }
    rendered_executions = json.loads(render_stimulus_executions(executions))
    assert [item["stimulus_id"] for item in rendered_executions["items"]] == [
        "ad-pressure",
        "checkout-traffic",
        "email-leak",
    ]
    assert span_reads == [(span_reads[0][0], 120)]
    assert events[-1] == "read-spans"
    assert span_reads[0][0].startswith("sentinel-stimulus/journey-")
    rendered = [" ".join(command) for command in commands]
    assert any("-n otel-demo patch configmap flagd-config" in item for item in rendered)
    assert any("-n otel-demo rollout restart deployment/flagd" in item for item in rendered)
    assert any("apply -f" in item and "ad-cpu-pressure.yaml" in item for item in rendered)
    assert any("delete -f" in item and "ad-cpu-pressure.yaml" in item for item in rendered)
    patches = [command[-1] for command in commands if "patch" in command]
    assert any(
        json.loads(json.loads(patch)["data"]["demo.flagd.json"])["flags"]["emailMemoryLeak"][
            "defaultVariant"
        ]
        == "100x"
        for patch in patches
    )
    assert '"defaultVariant": "off"' in json.loads(patches[-1])["data"]["demo.flagd.json"]


def test_live_stimulus_failure_still_restores_flagd() -> None:
    schedule = _fault_schedule(flag_variant="100x", flag_id="a-flag", chaos_id="z-chaos")
    commands: list[list[str]] = []
    anchor = datetime(2026, 7, 22, 12, 0, tzinfo=UTC)

    def runner(command: list[str], *, input_text: str | None = None, timeout: int = 120) -> str:
        del input_text, timeout
        commands.append(command)
        if "get" in command and "configmap" in command:
            return _flag_config_map()
        if "apply" in command:
            raise RuntimeError("injection failed")
        return ""

    with pytest.raises(RuntimeError, match="injection failed"):
        execute_stimuli(
            repo_root=SCENARIO_ROOT.parents[1],
            schedule=schedule,
            anchor_ts=anchor,
            runner=runner,
            waiter=lambda _: None,
            clock=lambda: anchor,
        )

    patches = [command[-1] for command in commands if "patch" in command]
    assert len(patches) == 2
    assert '"defaultVariant": "off"' in json.loads(patches[-1])["data"]["demo.flagd.json"]
    assert any(
        "delete" in command and any("ad-cpu-pressure.yaml" in item for item in command)
        for command in commands
    )


def test_unknown_flag_variant_fails_before_any_mutation() -> None:
    commands: list[list[str]] = []

    def runner(command: list[str], *, input_text: str | None = None, timeout: int = 120) -> str:
        del input_text, timeout
        commands.append(command)
        if "get" in command and "configmap" in command:
            return _flag_config_map()
        return ""

    with pytest.raises(ValueError, match="unknown flagd variant"):
        execute_stimuli(
            repo_root=SCENARIO_ROOT.parents[1],
            schedule=_fault_schedule(flag_variant="not-real"),
            anchor_ts=datetime(2026, 7, 22, 12, 0, tzinfo=UTC),
            runner=runner,
            waiter=lambda _: None,
            clock=lambda: datetime(2026, 7, 22, 12, 0, tzinfo=UTC),
        )

    assert not any("patch" in command for command in commands)


def _fault_schedule(
    *,
    flag_variant: str,
    flag_id: str = "email-leak",
    chaos_id: str = "ad-pressure",
    include_journey: bool = False,
) -> CompiledSchedule:
    profile = load_profile(SCENARIO_ROOT / "quiet_day.yml")
    document = profile.model_dump(mode="json")
    document["stimuli"] = [
        {
            "stimulus_id": flag_id,
            "kind": "flagd",
            "start_offset_seconds": 64,
            "duration_seconds": 20,
            "flag": "emailMemoryLeak",
            "variant": flag_variant,
        },
        {
            "stimulus_id": chaos_id,
            "kind": "chaos_mesh",
            "start_offset_seconds": 64,
            "duration_seconds": 20,
            "experiment": "ad-cpu-pressure",
        },
    ]
    if include_journey:
        document["stimuli"].append(
            {
                "stimulus_id": "checkout-traffic",
                "kind": "k6_journey",
                "start_offset_seconds": 64,
                "duration_seconds": 20,
                "journey": "checkout",
                "rate_rps": 2,
            }
        )
    return compile_profile(
        ScenarioProfile.model_validate(document),
        seed=profile.seeds.development[0],
        purpose=SeedPurpose.DEVELOPMENT,
    ).schedule


def _flag_config_map() -> str:
    config = {
        "$schema": "https://flagd.dev/schema/v0/flags.json",
        "flags": {
            "emailMemoryLeak": {
                "state": "ENABLED",
                "variants": {"off": 0, "100x": 100},
                "defaultVariant": "off",
            }
        },
    }
    return json.dumps(
        {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "data": {"demo.flagd.json": json.dumps(config, indent=2) + "\n"},
        }
    )
