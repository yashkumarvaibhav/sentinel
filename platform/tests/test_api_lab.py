"""The demo launcher records intent and never does the work.

The safety property under test is a boundary, not a behaviour: the process
serving this endpoint has no `lab/` in its image, so the tests below are mostly
about what the endpoint refuses and what it truthfully reports when the thing
that *can* do the work is not there.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from api.app import create_app
from api.lab import (
    SCENARIOS,
    LabRunRefusedError,
    build_lab_run,
    lab_run_feed,
    parse_lab_request,
)
from common.settings import Settings
from contracts import (
    LabRunFeedStatus,
    LabRunMode,
    LabRunSnapshot,
    LabRunState,
    LabScenarioRequest,
)

TS = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)
REPO_ROOT = Path(__file__).resolve().parents[2]
SCENARIO_ROOT = REPO_ROOT / "lab" / "scenarios"


class _Queue:
    """An in-memory stand-in with the one-in-flight rule the index enforces."""

    def __init__(self, runs: list[LabRunSnapshot] | None = None) -> None:
        self.runs = runs or []

    async def enqueue_lab_run(self, run: LabRunSnapshot) -> bool:
        if any(
            existing.state in {LabRunState.QUEUED, LabRunState.RUNNING} for existing in self.runs
        ):
            return False
        self.runs.insert(0, run)
        return True

    async def list_lab_runs(self, *, limit: int) -> tuple[LabRunSnapshot, ...]:
        return tuple(self.runs[:limit])


def _client(*, queue: _Queue | None = None, runner: bool = True) -> TestClient:
    settings = Settings().model_copy(
        update={"config_dir": REPO_ROOT / "config", "lab_runner_attached": runner}
    )
    app = create_app(
        settings,
        probes={},
        lab_run_store=queue if queue is not None else _Queue(),
    )
    return TestClient(app)


# --- the catalogue is honest about the profiles it names ---------------------


def test_every_offered_scenario_exists_as_a_committed_profile() -> None:
    """A button that queues a scenario nothing can run is a broken button."""
    for option in SCENARIOS:
        profile = SCENARIO_ROOT / f"{option.scenario_id}.yml"
        assert profile.is_file(), f"{option.scenario_id} has no committed profile"


def test_the_stated_live_duration_is_the_profiles_own() -> None:
    """The number on the button is the sum of the profile's load phases."""
    for option in SCENARIOS:
        if option.live_duration_seconds is None:
            continue
        document = yaml.safe_load((SCENARIO_ROOT / f"{option.scenario_id}.yml").read_text())
        measured = sum(int(phase["duration_seconds"]) for phase in document["load_phases"])
        assert option.live_duration_seconds == measured, (
            f"{option.scenario_id} says {option.live_duration_seconds}s but its phases "
            f"total {measured}s"
        )


def test_every_offered_scenario_commits_a_development_seed() -> None:
    """A demo never spends a held-out seed, so one has to exist to spend instead."""
    for option in SCENARIOS:
        document = yaml.safe_load((SCENARIO_ROOT / f"{option.scenario_id}.yml").read_text())
        assert document["seeds"]["development"], f"{option.scenario_id} has no development seed"


# --- what the request may say ------------------------------------------------


def test_a_scenario_this_deployment_does_not_offer_is_refused() -> None:
    with pytest.raises(LabRunRefusedError, match="not a scenario this deployment offers"):
        build_lab_run(LabScenarioRequest(scenario_id="not_a_scenario", mode=LabRunMode.LIVE), ts=TS)


def test_a_mode_the_scenario_does_not_offer_is_refused() -> None:
    """Only combo_night has a committed capture, so only it can be replayed."""
    with pytest.raises(LabRunRefusedError, match="cannot be fired in REPLAY"):
        build_lab_run(LabScenarioRequest(scenario_id="quiet_day", mode=LabRunMode.REPLAY), ts=TS)


def test_a_body_naming_fields_a_client_may_not_set_is_refused() -> None:
    with pytest.raises(LabRunRefusedError):
        parse_lab_request({"scenario_id": "combo_night", "mode": "LIVE", "state": "SUCCEEDED"})


def test_a_queued_run_says_plainly_that_this_endpoint_executed_nothing() -> None:
    run = build_lab_run(
        LabScenarioRequest(scenario_id="combo_night", mode=LabRunMode.REPLAY), ts=TS
    )

    assert run.state is LabRunState.QUEUED
    assert run.started_at is None and run.finished_at is None
    assert "never executes" in run.detail
    assert "recorded" in run.honesty.telemetry.lower()


def test_replay_and_live_do_not_claim_the_same_honesty() -> None:
    """A launcher that presented them the same invites reading a recording as live."""
    replay = build_lab_run(
        LabScenarioRequest(scenario_id="combo_night", mode=LabRunMode.REPLAY), ts=TS
    )
    live = build_lab_run(LabScenarioRequest(scenario_id="combo_night", mode=LabRunMode.LIVE), ts=TS)

    assert replay.honesty != live.honesty
    assert "Bit-exact" in replay.honesty.reproducibility
    assert "Statistical" in live.honesty.reproducibility


# --- what the feed reports ---------------------------------------------------


def test_a_launcher_with_no_runner_says_so_rather_than_queueing_dead_work() -> None:
    feed = lab_run_feed((), runner_attached=False)

    assert feed.status is LabRunFeedStatus.UNAVAILABLE
    assert not feed.runner_attached
    assert "nothing will" in feed.note


def test_a_launcher_with_a_run_in_flight_is_busy_and_says_why() -> None:
    running = build_lab_run(
        LabScenarioRequest(scenario_id="combo_night", mode=LabRunMode.LIVE), ts=TS
    ).model_copy(update={"state": LabRunState.RUNNING, "started_at": TS})

    feed = lab_run_feed((running,), runner_attached=True)

    assert feed.status is LabRunFeedStatus.BUSY
    assert "two sets of injected faults" in feed.note


# --- the endpoint itself -----------------------------------------------------


def test_firing_a_scenario_queues_a_row_and_returns_the_feed() -> None:
    queue = _Queue()
    with _client(queue=queue) as client:
        answer = client.post(
            "/api/lab/scenario", json={"scenario_id": "combo_night", "mode": "REPLAY"}
        )

    assert answer.status_code == 200
    body = answer.json()
    assert body["status"] == "BUSY", "the run it just queued is in flight"
    assert len(queue.runs) == 1
    assert queue.runs[0].state == "QUEUED"


def test_a_second_scenario_is_refused_while_one_is_in_flight() -> None:
    queue = _Queue()
    with _client(queue=queue) as client:
        first = client.post(
            "/api/lab/scenario", json={"scenario_id": "combo_night", "mode": "REPLAY"}
        )
        second = client.post(
            "/api/lab/scenario", json={"scenario_id": "combo_night", "mode": "LIVE"}
        )

    assert first.status_code == 200
    assert second.status_code == 409
    assert len(queue.runs) == 1


def test_firing_a_scenario_with_no_runner_attached_queues_nothing() -> None:
    queue = _Queue()
    with _client(queue=queue, runner=False) as client:
        answer = client.post(
            "/api/lab/scenario", json={"scenario_id": "combo_night", "mode": "REPLAY"}
        )

    assert answer.status_code == 409
    assert answer.json()["runner_attached"] is False
    assert queue.runs == []


def test_a_scenario_this_deployment_will_not_fire_answers_400() -> None:
    with _client() as client:
        answer = client.post(
            "/api/lab/scenario", json={"scenario_id": "not_a_scenario", "mode": "LIVE"}
        )

    assert answer.status_code == 400
    assert answer.json()["status"] == "UNAVAILABLE"


def test_the_feed_reads_without_firing_anything() -> None:
    queue = _Queue()
    with _client(queue=queue) as client:
        answer = client.get("/api/lab/runs")

    assert answer.status_code == 200
    assert answer.json()["status"] == "READY"
    assert queue.runs == []


def test_a_gateway_with_no_queue_attached_answers_503_rather_than_empty() -> None:
    settings = Settings().model_copy(
        update={"config_dir": REPO_ROOT / "config", "lab_runner_attached": True}
    )
    with TestClient(create_app(settings, probes={})) as client:
        answer = client.get("/api/lab/runs")

    assert answer.status_code == 503
    assert answer.json()["status"] == "UNAVAILABLE"


def test_the_gateway_never_imports_the_lab_package() -> None:
    """The whole reason this endpoint only records intent.

    `deploy/gateway.Dockerfile` copies `platform/` and `config/` and nothing
    else, so a gateway that reached for `lab` would not merely be impolite - it
    would fail to start. The check is here rather than in a comment because the
    import that breaks it would look harmless in review.
    """
    import ast
    from collections import deque

    platform_root = REPO_ROOT / "platform"
    seen: set[str] = set()
    queue = deque(["api.app"])
    violations: list[str] = []
    while queue:
        module = queue.popleft()
        if module in seen:
            continue
        seen.add(module)
        candidate = platform_root.joinpath(*module.split("."))
        path = (
            candidate.with_suffix(".py")
            if candidate.with_suffix(".py").is_file()
            else candidate / "__init__.py"
            if (candidate / "__init__.py").is_file()
            else None
        )
        if path is None:
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module is not None:
                names = [node.module]
            else:
                continue
            for name in names:
                if name.split(".")[0] == "lab":
                    violations.append(f"{module} -> {name}")
                queue.append(name)

    assert violations == []


def test_a_finished_run_cannot_claim_to_still_be_going() -> None:
    run = build_lab_run(
        LabScenarioRequest(scenario_id="combo_night", mode=LabRunMode.REPLAY), ts=TS
    )

    with pytest.raises(ValueError, match="finished time and a terminal state"):
        run.model_copy(update={"finished_at": TS + timedelta(seconds=1)}).model_validate(
            run.model_copy(update={"finished_at": TS + timedelta(seconds=1)}).model_dump()
        )
