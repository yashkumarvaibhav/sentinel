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
    MAX_STIMULUS_DRIFT_SECONDS,
    StimulusDriftError,
    StimulusExecution,
    build_checkout_journey_job,
    build_k6_job,
    build_path_attack_job,
    execute_stimuli,
    expected_request_count,
    render_stimulus_executions,
    stimulus_drift_seconds,
    validate_stimulus_drift,
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
    assert env["SENTINEL_RATE_RPS"] == "15"
    assert env["SENTINEL_USER_AGENT"] == "sentinel-score/run-combo/attack/abc"
    assert expected_request_count(schedule) == 10_796


def test_primary_rate_phase_records_anchored_boundaries_without_a_second_job() -> None:
    # 6d-3: the rate phase spawns no job and its DROP window is the primary's
    # scheduled low-rate phase, which runs anchored to the marker burst. The
    # execution must be recorded at anchor+offset (drift-free), NEVER from the
    # loop's wall clock -- so a wildly wrong clock must be ignored entirely.
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

    def runner(
        command: list[str],
        *,
        input_text: str | None = None,
        timeout: int = 120,
    ) -> str:
        del input_text, timeout
        commands.append(command)
        return ""

    def clock() -> datetime:
        raise AssertionError("the anchored rate phase must not read the wall clock")

    executions = execute_stimuli(
        repo_root=SCENARIO_ROOT.parents[1],
        schedule=minimal,
        anchor_ts=anchor,
        runner=runner,
        waiter=lambda _: None,
        clock=clock,
    )

    assert commands == []
    assert executions[0].kind == "k6_rate_phase"
    assert executions[0].setting == "primary@2rps"
    assert executions[0].started_at == anchor + timedelta(seconds=rate_phase.start_offset_seconds)
    assert executions[0].ended_at == anchor + timedelta(
        seconds=rate_phase.start_offset_seconds + rate_phase.duration_seconds
    )


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


def _execution(
    stimulus_id: str,
    *,
    anchor: datetime,
    requested_start: int,
    requested_end: int,
    start_drift: float,
    end_drift: float,
) -> StimulusExecution:
    return StimulusExecution(
        stimulus_id=stimulus_id,
        kind="flagd",
        target="otel-demo/paymentFailure",
        setting="100%",
        requested_start_offset_seconds=requested_start,
        requested_end_offset_seconds=requested_end,
        started_at=anchor + timedelta(seconds=requested_start + start_drift),
        ended_at=anchor + timedelta(seconds=requested_end + end_drift),
    )


def test_stimulus_drift_seconds_reports_signed_start_and_end_drift() -> None:
    anchor = datetime(2026, 7, 22, 12, 0, tzinfo=UTC)
    execution = _execution(
        "payment_failure",
        anchor=anchor,
        requested_start=304,
        requested_end=484,
        start_drift=18.0,
        end_drift=-3.0,
    )

    assert stimulus_drift_seconds(execution, anchor=anchor) == (18.0, -3.0)


def test_legitimate_spin_up_drift_is_accepted() -> None:
    anchor = datetime(2026, 7, 22, 12, 0, tzinfo=UTC)
    # 14-40 s job spin-up is expected and must not fail the capture.
    executions = (
        _execution(
            "payment_failure",
            anchor=anchor,
            requested_start=304,
            requested_end=484,
            start_drift=38.0,
            end_drift=2.0,
        ),
    )

    validate_stimulus_drift(executions, anchor=anchor)  # does not raise


def test_compounding_back_half_drift_fails_the_capture_closed() -> None:
    anchor = datetime(2026, 7, 22, 12, 0, tzinfo=UTC)
    # The 9221 shape: the back-half stimulus ran ~90 s past its scheduled window.
    executions = (
        _execution(
            "measured_rate_drop",
            anchor=anchor,
            requested_start=664,
            requested_end=724,
            start_drift=90.0,
            end_drift=90.0,
        ),
    )

    with pytest.raises(StimulusDriftError, match="isolation bound"):
        validate_stimulus_drift(executions, anchor=anchor)


def test_drift_bound_boundary_is_inclusive() -> None:
    anchor = datetime(2026, 7, 22, 12, 0, tzinfo=UTC)
    at_bound = (
        _execution(
            "payment_failure",
            anchor=anchor,
            requested_start=304,
            requested_end=484,
            start_drift=MAX_STIMULUS_DRIFT_SECONDS,
            end_drift=0.0,
        ),
    )
    validate_stimulus_drift(at_bound, anchor=anchor)  # exactly at the bound passes

    over = (
        _execution(
            "payment_failure",
            anchor=anchor,
            requested_start=304,
            requested_end=484,
            start_drift=MAX_STIMULUS_DRIFT_SECONDS + 0.5,
            end_drift=0.0,
        ),
    )
    with pytest.raises(StimulusDriftError):
        validate_stimulus_drift(over, anchor=anchor)


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


def test_journey_reaping_is_deferred_past_the_transition_loop() -> None:
    # 6d-1: a journey's blocking reap (_wait_for_job/log/delete) must not run inside
    # the transition loop, or a stimulus starting at/after the journey's stop offset
    # fires late -- the compounding back-half drift that broke combo-9221's isolation.
    # Structural proof: a journey stops at 84 while a flag stops at 104, so the loop's
    # last wait is 104. The journey reap (get job) must happen AFTER wait:104 (deferred
    # past the loop), and span verification must still be the final action.
    schedule = _journey_with_trailing_stop_schedule()
    events: list[str] = []
    anchor = datetime(2026, 7, 22, 12, 0, tzinfo=UTC)

    def runner(command: list[str], *, input_text: str | None = None, timeout: int = 120) -> str:
        del input_text, timeout
        if "get" in command and "configmap" in command:
            return _flag_config_map()
        if "get" in command and "job" in command:
            events.append("reap-getjob")
            return '{"status":{"succeeded":1}}'
        if "logs" in command:
            return "iterations dropped_iterations"
        return ""

    def waiter(target: datetime) -> None:
        events.append(f"wait:{int((target - anchor).total_seconds())}")

    def read_spans(user_agent: str, expected: int) -> tuple[datetime, ...]:
        del user_agent
        events.append("read-spans")
        return tuple(
            anchor + timedelta(seconds=66, milliseconds=index)
            for index in range(math.ceil(expected * 0.95))
        )

    execute_stimuli(
        repo_root=SCENARIO_ROOT.parents[1],
        schedule=schedule,
        anchor_ts=anchor,
        runner=runner,
        waiter=waiter,
        clock=lambda: anchor + timedelta(seconds=65),
        journey_span_reader=read_spans,
    )

    assert "wait:104" in events
    assert events.index("wait:104") < events.index("reap-getjob")
    assert events[-1] == "read-spans"


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


def _journey_with_trailing_stop_schedule() -> CompiledSchedule:
    # A checkout journey [64,84] plus an emailMemoryLeak flag [64,104]; the extended
    # observe phase keeps both stimulus windows inside the scenario duration so the
    # loop has a stop offset (104) after the journey's (84).
    profile = load_profile(SCENARIO_ROOT / "quiet_day.yml")
    document = profile.model_dump(mode="json")
    document["load_phases"] = [
        {"name": "warmup", "duration_seconds": 64, "rate_rps": 4},
        {"name": "observe", "duration_seconds": 60, "rate_rps": 4},
    ]
    document["stimuli"] = [
        {
            "stimulus_id": "checkout-traffic",
            "kind": "k6_journey",
            "start_offset_seconds": 64,
            "duration_seconds": 20,
            "journey": "checkout",
            "rate_rps": 2,
        },
        {
            "stimulus_id": "email-leak",
            "kind": "flagd",
            "start_offset_seconds": 64,
            "duration_seconds": 40,
            "flag": "emailMemoryLeak",
            "variant": "100x",
        },
    ]
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


def test_span_wait_survives_a_transient_query_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    start = datetime(2026, 7, 22, 12, 0, tzinfo=UTC)
    full = tuple(start + timedelta(seconds=index) for index in range(20))
    responses: list[RuntimeError | tuple[datetime, ...]] = [
        RuntimeError("command failed (docker): transient exec error"),
        full,
    ]

    def query_spans(*, repo_root: Path, user_agent: str) -> tuple[datetime, ...]:
        del repo_root, user_agent
        item = responses.pop(0)
        if isinstance(item, RuntimeError):
            raise item
        return item

    monkeypatch.setattr(live_module, "_query_spans", query_spans)
    monkeypatch.setattr("lab.scoring.live.time.sleep", lambda _: None)

    timestamps = live_module._wait_for_spans(
        repo_root=SCENARIO_ROOT.parents[1],
        user_agent="sentinel-stimulus/journey-test",
        expected=20,
    )

    assert timestamps == full
    assert responses == []


def test_span_wait_fails_closed_when_queries_never_succeed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ticks = (float(value) for value in range(0, 400, 10))

    def query_spans(*, repo_root: Path, user_agent: str) -> tuple[datetime, ...]:
        del repo_root, user_agent
        raise RuntimeError("command failed (docker): exec error")

    monkeypatch.setattr(live_module, "_query_spans", query_spans)
    monkeypatch.setattr("lab.scoring.live.time.sleep", lambda _: None)
    monkeypatch.setattr("lab.scoring.live.time.monotonic", lambda: next(ticks))

    with pytest.raises(RuntimeError, match="never succeeded"):
        live_module._wait_for_spans(
            repo_root=SCENARIO_ROOT.parents[1],
            user_agent="sentinel-stimulus/journey-test",
            expected=5,
        )


def test_live_anchor_survives_a_transient_query_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    start = datetime(2026, 7, 22, 12, 0, tzinfo=UTC)
    burst = tuple(start + timedelta(milliseconds=index) for index in range(10))
    responses: list[RuntimeError | tuple[datetime, ...]] = [
        RuntimeError("command failed (docker): transient exec error"),
        burst,
    ]

    def query_spans(*, repo_root: Path, user_agent: str) -> tuple[datetime, ...]:
        del repo_root, user_agent
        item = responses.pop(0)
        if isinstance(item, RuntimeError):
            raise item
        return item

    monkeypatch.setattr(live_module, "_query_spans", query_spans)
    monkeypatch.setattr("lab.scoring.live.time.sleep", lambda _: None)

    anchor = live_module._wait_for_anchor(
        repo_root=SCENARIO_ROOT.parents[1],
        user_agent="sentinel-score-anchor/run-test",
    )

    assert anchor == start + timedelta(milliseconds=9)
    assert responses == []
