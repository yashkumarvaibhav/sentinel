"""The demo launcher's server side: record the intent, never do the work.

Which scenarios exist and how long they take is read from the committed
profiles - but the gateway image has no ``lab/``, so it cannot read them
either. What it has instead is this module's committed catalogue, kept
deliberately small: an id, a sentence, and the modes each id may be fired in.
A mismatch between it and the profiles is caught by a test rather than by a
button that queues work no runner can execute.

The honesty text is not decoration. A replay and a live run look identical
once an incident lands, and a launcher that presented them the same way would
be inviting an audience to read a recording as a live system.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Protocol

from pydantic import BaseModel, ValidationError

from contracts import (
    LabRunControl,
    LabRunControlRequest,
    LabRunFeed,
    LabRunFeedStatus,
    LabRunHonesty,
    LabRunMode,
    LabRunnerHeartbeat,
    LabRunSnapshot,
    LabRunState,
    LabScenarioOption,
    LabScenarioRequest,
)

# How many past runs the launcher shows. A demo surface is a short history.
MAX_LAB_RUNS = 20

# How long a run may sit in a non-terminal state before the launcher stops
# treating it as in flight.
#
# The lease exists so two scenarios cannot inject faults into one stretch of
# telemetry (decision #135). But a QUEUED row only becomes RUNNING when a
# runner claims it, and nothing obliges a runner to exist - stop the
# ``demo`` profile, or let it die mid-run, and the row stays non-terminal
# forever. The launcher then reports "already running" about a run that is
# not running, and refuses every future scenario on the strength of it.
#
# That is worse than a stuck button: it is the surface stating something
# untrue about the platform's own state, which is the one thing this project
# does not do. So a run past these bounds is reported as ABANDONED - named,
# with its age - and stops holding the lease.
#
# The bounds are generous on purpose. A replay of the longest committed
# scenario runs in minutes, and a live run takes the wall-clock time its
# profile states; being slow must never be mistaken for being dead.
UNCLAIMED_AFTER = timedelta(minutes=15)
RUNNING_AFTER = timedelta(hours=2)
RUNNER_STALE_AFTER = timedelta(seconds=20)


def _abandoned(run: LabRunSnapshot, *, now: datetime) -> bool:
    """Whether a non-terminal run has aged out of being believable."""
    if run.state is LabRunState.QUEUED:
        bound = UNCLAIMED_AFTER
    elif run.state is LabRunState.RUNNING:
        bound = RUNNING_AFTER
    else:
        return False
    requested = run.requested_at
    if requested.tzinfo is None:
        requested = requested.replace(tzinfo=UTC)
    return now - requested > bound


def _describe_age(delta: timedelta) -> str:
    """A coarse age. The exact seconds do not change what an operator does."""
    hours = int(delta.total_seconds() // 3600)
    if hours >= 24:
        days = hours // 24
        return f"{days} day{'s' if days != 1 else ''}"
    if hours >= 1:
        return f"{hours} hour{'s' if hours != 1 else ''}"
    minutes = max(int(delta.total_seconds() // 60), 1)
    return f"{minutes} minute{'s' if minutes != 1 else ''}"


REPLAY_HONESTY = LabRunHonesty(
    telemetry="REAL — recorded from the testbed, replayed byte for byte.",
    stimulus="SIMULATED — the faults and the attack were injected when this was recorded.",
    reproducibility="Bit-exact: the same capture always produces the same decisions.",
)

LIVE_HONESTY = LabRunHonesty(
    telemetry="REAL — measured from the testbed while this ran.",
    stimulus="SIMULATED — the faults and the attack are injected into a contained cluster.",
    reproducibility="Statistical only: a live run is judged with tolerance bands, never exactly.",
)


class LabRunStore(Protocol):
    """The durable queue the launcher writes to and reads back."""

    async def enqueue_lab_run(self, run: LabRunSnapshot) -> bool: ...

    async def list_lab_runs(self, *, limit: int) -> tuple[LabRunSnapshot, ...]: ...

    async def control_lab_run(
        self,
        *,
        run_id: str,
        control: LabRunControl,
        ts: datetime,
    ) -> LabRunSnapshot | None: ...

    async def latest_lab_runner_heartbeat(self) -> LabRunnerHeartbeat | None: ...


class LabRunRefusedError(ValueError):
    """The request is well formed but this deployment will not carry it out."""


# The scenarios a client may fire, and what each one costs. `live_duration_seconds`
# is the sum of the profile's own load phases; the test beside this module keeps
# both halves of that claim honest against `lab/scenarios/*.yml`.
SCENARIOS: tuple[LabScenarioOption, ...] = (
    LabScenarioOption(
        scenario_id="combo_night",
        name="Championship night",
        description=(
            "The north-star shape: an attack, a payment fault, resource pressure, a "
            "measured drop and an emitter silence, all inside an explained match surge."
        ),
        modes=(LabRunMode.REPLAY, LabRunMode.LIVE),
        live_duration_seconds=1004,
    ),
    LabScenarioOption(
        scenario_id="match_night",
        name="Match night, nothing wrong",
        description=(
            "A legitimate surge and nothing else. The platform should explain all of "
            "it and act on none of it."
        ),
        modes=(LabRunMode.LIVE,),
        live_duration_seconds=100,
    ),
    LabScenarioOption(
        scenario_id="attack_day",
        name="Attack, no event",
        description="Hostile traffic on an ordinary day, with no surge to hide inside.",
        modes=(LabRunMode.LIVE,),
        live_duration_seconds=84,
    ),
    LabScenarioOption(
        scenario_id="cascade_night",
        name="Cascading fault",
        description="One failing dependency, and the retry storm it pulls up the graph.",
        modes=(LabRunMode.LIVE,),
        live_duration_seconds=144,
    ),
    LabScenarioOption(
        scenario_id="quiet_day",
        name="Quiet day",
        description="Nothing happening, which is the hardest thing to report correctly.",
        modes=(LabRunMode.LIVE,),
        live_duration_seconds=84,
    ),
)

_BY_ID = {option.scenario_id: option for option in SCENARIOS}


def build_lab_run(request: LabScenarioRequest, *, ts: datetime) -> LabRunSnapshot:
    """Turn a client's intent into the queued row, refusing what is not offered."""
    option = _BY_ID.get(request.scenario_id)
    if option is None:
        raise LabRunRefusedError(f"{request.scenario_id} is not a scenario this deployment offers")
    if request.mode not in option.modes:
        offered = ", ".join(mode.value for mode in option.modes)
        raise LabRunRefusedError(
            f"{option.scenario_id} cannot be fired in {request.mode.value}; it offers {offered}"
        )
    return LabRunSnapshot(
        run_id=f"run-{uuid.uuid4().hex}",
        scenario_id=option.scenario_id,
        mode=request.mode,
        state=LabRunState.QUEUED,
        requested_at=ts,
        detail=(
            f"Queued {option.name} in {request.mode.value.lower()} mode. "
            "The runner claims it; this endpoint never executes one."
        ),
        honesty=REPLAY_HONESTY if request.mode is LabRunMode.REPLAY else LIVE_HONESTY,
    )


class ScenarioActivity(BaseModel):
    """Current execution or latest replay window, for the public console.

    The rest of ``/api/lab`` is gated because firing is mutating and the
    catalogue is close to scenario ground truth. *That a run is in flight* is
    neither: it is a fact about the platform's own state, and the command
    centre looks broken without it — during a replay nothing is written until
    the run ends, so a screen that cannot see the run shows a still page for
    minutes and gives an operator no reason to believe anything is happening.

    Deliberately narrow: no scenario list, no seeds, no detail that would let a
    reader infer what was injected. The completed replay coordinates are kept
    so the chart remains on the evidence the operator just asked to see.
    """

    in_flight: bool
    run_id: str | None = None
    scenario_id: str | None = None
    mode: str | None = None
    state: str | None = None
    started_at: datetime | None = None
    evidence_start_at: datetime | None = None
    evidence_end_at: datetime | None = None
    evidence_cursor_at: datetime | None = None
    progress: float | None = None
    note: str


def scenario_activity(
    runs: tuple[LabRunSnapshot, ...],
    *,
    now: datetime | None = None,
) -> ScenarioActivity:
    """The active run, or the latest completed replay evidence window."""
    moment = now if now is not None else datetime.now(UTC)
    live = [
        run
        for run in runs
        if run.state in {LabRunState.QUEUED, LabRunState.RUNNING, LabRunState.PAUSED}
        and not _abandoned(run, now=moment)
    ]
    if not live:
        latest_replay = next(
            (
                run
                for run in runs
                if run.mode is LabRunMode.REPLAY
                and run.state is LabRunState.SUCCEEDED
                and run.evidence_start_at is not None
            ),
            None,
        )
        if latest_replay is not None:
            return ScenarioActivity(
                in_flight=False,
                run_id=latest_replay.run_id,
                scenario_id=latest_replay.scenario_id,
                mode=latest_replay.mode.value,
                state=latest_replay.state.value,
                started_at=latest_replay.started_at,
                evidence_start_at=latest_replay.evidence_start_at,
                evidence_end_at=latest_replay.evidence_end_at,
                evidence_cursor_at=latest_replay.evidence_cursor_at,
                progress=latest_replay.progress,
                note="Showing the most recently completed replay evidence.",
            )
        return ScenarioActivity(in_flight=False, note="No scenario is running.")
    run = live[0]
    started = run.started_at if run.started_at is not None else run.requested_at
    return ScenarioActivity(
        in_flight=True,
        run_id=run.run_id,
        scenario_id=run.scenario_id,
        mode=run.mode.value,
        state=run.state.value,
        started_at=started,
        evidence_start_at=run.evidence_start_at,
        evidence_end_at=run.evidence_end_at,
        evidence_cursor_at=run.evidence_cursor_at,
        progress=run.progress,
        note=(f"{run.scenario_id} is {run.state.value.lower()} in {run.mode.value.lower()} mode."),
    )


def lab_run_feed(
    runs: tuple[LabRunSnapshot, ...],
    *,
    runner_attached: bool,
    live_ready: bool | None = None,
    runner_detail: str | None = None,
    now: datetime | None = None,
) -> LabRunFeed:
    """The launcher's whole view, including why it may not be usable."""
    moment = now if now is not None else datetime.now(UTC)
    can_run_live = runner_attached if live_ready is None else live_ready
    capability_detail = runner_detail or (
        "Runner heartbeat is current." if runner_attached else "No current runner heartbeat."
    )
    non_terminal = [
        run
        for run in runs
        if run.state in {LabRunState.QUEUED, LabRunState.RUNNING, LabRunState.PAUSED}
    ]
    abandoned = [run for run in non_terminal if _abandoned(run, now=moment)]
    in_flight = [run for run in non_terminal if run not in abandoned]

    if abandoned and not in_flight and runner_attached:
        stalest = min(
            abandoned,
            key=lambda run: (
                run.requested_at.replace(tzinfo=UTC)
                if run.requested_at.tzinfo is None
                else run.requested_at
            ),
        )
        requested = stalest.requested_at
        if requested.tzinfo is None:
            requested = requested.replace(tzinfo=UTC)
        age = _describe_age(moment - requested)
        verb = (
            "was never claimed by a runner"
            if stalest.state is LabRunState.QUEUED
            else "stopped reporting"
        )
        return LabRunFeed(
            status=LabRunFeedStatus.READY,
            scenarios=SCENARIOS,
            runs=runs,
            runner_attached=runner_attached,
            live_ready=can_run_live,
            runner_detail=capability_detail,
            note=(
                f"{stalest.scenario_id} {verb} and has been {stalest.state.value.lower()} "
                f"for {age}, so it is treated as abandoned rather than running. "
                "Firing a scenario is safe; its telemetry was never produced."
            ),
        )

    if not runner_attached:
        status = LabRunFeedStatus.UNAVAILABLE
        note = (
            "No lab runner is attached, so a request would queue work nothing will "
            "ever claim. Start one beside the stack with `make demo-runner`."
        )
    elif in_flight:
        status = LabRunFeedStatus.BUSY
        if in_flight[0].state is LabRunState.PAUSED:
            note = (
                f"{in_flight[0].scenario_id} is paused and still owns the scenario slot. "
                "Resume it from the beginning or stop it before firing another scenario."
            )
        else:
            note = (
                f"{in_flight[0].scenario_id} is already running. A second scenario would put "
                "two sets of injected faults into one stretch of telemetry, and neither "
                "run's evidence would mean anything afterwards."
            )
    elif not can_run_live:
        status = LabRunFeedStatus.READY
        note = (
            "Recorded replay is ready. Live mode is unavailable and will not be queued: "
            f"{capability_detail}"
        )
    else:
        status = LabRunFeedStatus.READY
        note = (
            "Pick a scenario. A replay is instant and bit-exact; a live run drives real "
            "load and real injected faults at the testbed, and takes the time it says."
        )
    return LabRunFeed(
        status=status,
        scenarios=SCENARIOS,
        runs=runs,
        runner_attached=runner_attached,
        live_ready=can_run_live,
        runner_detail=capability_detail,
        note=note,
    )


def unavailable_lab_feed(*, detail: str) -> LabRunFeed:
    """The honest empty view when the queue itself cannot be read."""
    return LabRunFeed(
        status=LabRunFeedStatus.UNAVAILABLE,
        scenarios=SCENARIOS,
        runs=(),
        runner_attached=False,
        live_ready=False,
        runner_detail=detail,
        note=detail,
    )


def lab_runner_readiness(
    heartbeat: LabRunnerHeartbeat | None,
    *,
    declared_attached: bool,
    now: datetime,
) -> tuple[bool, bool, str]:
    """Prefer a current measured heartbeat over the legacy declaration."""
    if heartbeat is None:
        if declared_attached:
            return (
                False,
                False,
                "The deployment declares a runner, but no runner heartbeat has arrived.",
            )
        return False, False, "No lab runner is configured for this deployment."
    seen_at = heartbeat.seen_at
    if seen_at.tzinfo is None:
        seen_at = seen_at.replace(tzinfo=UTC)
    age = now - seen_at
    if age > RUNNER_STALE_AFTER:
        return (
            False,
            False,
            f"The last runner heartbeat is {_describe_age(age)} old.",
        )
    return True, heartbeat.live_ready, heartbeat.detail


def parse_lab_request(body: object) -> LabScenarioRequest:
    """Validate a client body strictly, refusing anything it may not say."""
    try:
        return LabScenarioRequest.model_validate(body)
    except ValidationError as error:
        raise LabRunRefusedError(
            f"the request names no scenario this launcher can fire: {error}"
        ) from (error)


def parse_lab_control_request(body: object) -> LabRunControlRequest:
    """Validate one operator control without accepting server-owned fields."""
    try:
        return LabRunControlRequest.model_validate(body)
    except ValidationError as error:
        raise LabRunRefusedError(f"the run control request is invalid: {error}") from error


def control_lab_run(
    run: LabRunSnapshot,
    *,
    control: LabRunControl,
    ts: datetime,
) -> LabRunSnapshot:
    """Apply the synchronous part of a control transition.

    A running pause/stop is an intent until the runner has unwound its owned
    resources. A queued or already-paused run has nothing active to clean, so
    the gateway-side store can settle it atomically.
    """
    if control is LabRunControl.PAUSE:
        if run.state is LabRunState.QUEUED:
            return run.model_copy(
                update={
                    "state": LabRunState.PAUSED,
                    "started_at": ts,
                    "detail": (
                        "Paused before a runner started it. Resume restarts the "
                        "schedule from the beginning."
                    ),
                }
            )
        if run.state is LabRunState.RUNNING:
            return run.model_copy(
                update={
                    "control_requested": control,
                    "detail": (
                        "Pause requested. The runner is safely unwinding the active stimulus."
                    ),
                }
            )
        if run.state is LabRunState.PAUSED:
            return run
        raise LabRunRefusedError(f"a {run.state.value.lower()} run cannot be paused")

    if control is LabRunControl.RESUME:
        if run.state is not LabRunState.PAUSED:
            raise LabRunRefusedError(f"a {run.state.value.lower()} run cannot be resumed")
        return run.model_copy(
            update={
                "state": LabRunState.QUEUED,
                "started_at": None,
                "control_requested": None,
                "evidence_start_at": None,
                "evidence_end_at": None,
                "evidence_cursor_at": None,
                "progress": None,
                "detail": (
                    "Queued to restart from the beginning; authored evidence windows are "
                    "never resumed halfway through."
                ),
            }
        )

    if run.state in {LabRunState.QUEUED, LabRunState.PAUSED}:
        return run.model_copy(
            update={
                "state": LabRunState.STOPPED,
                "finished_at": max(ts, run.started_at or run.requested_at),
                "control_requested": None,
                "detail": "Stopped by the operator; no scenario stimulus remains active.",
            }
        )
    if run.state is LabRunState.RUNNING:
        return run.model_copy(
            update={
                "control_requested": control,
                "detail": "Stop requested. The runner is safely unwinding the active stimulus.",
            }
        )
    if run.state is LabRunState.STOPPED:
        return run
    raise LabRunRefusedError(f"a {run.state.value.lower()} run cannot be stopped")


def run_payload(run: LabRunSnapshot) -> dict[str, object]:
    """Canonical JSON for a run, for the runner CLI's own output."""
    return dict(json.loads(run.model_dump_json()))
