"""Public, intent-only contract for firing a scenario at the testbed.

The demo launcher is the one place a person can make the platform *do*
something from a browser, so its contract is drawn the same way the action
control's is: the client says what it wants, and nothing else.

**The endpoint records intent; it never executes.** The gateway image carries
no ``lab/`` at all - that boundary is what keeps scenario ground truth
unreachable from the process that serves the public API, and it is not being
loosened for a demo button. So a request becomes a queued row, and a lab-side
runner with the repo mounted claims it and does the work. The command centre
finds out the same way it finds out about everything else: a post-commit
invalidation.

**One run at a time.** A second scenario fired while one is running would put
two sets of injected faults into one stretch of telemetry, and every label
either of them carries would then describe traffic the other one also caused.
That is not a queue-depth preference; it is what makes a scored run mean
anything.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Self

from pydantic import Field, field_validator, model_validator

from contracts._base import ContractModel, HumanText, Identifier, UtcDatetime


class LabRunMode(StrEnum):
    """How a scenario is put in front of the platform."""

    # Re-drive a committed recording. Instant and bit-exact, which is what makes
    # it the mode a demo can be given in front of people.
    REPLAY = "REPLAY"
    # Drive real load and real injected faults at the running testbed. Real
    # minutes, and only statistically reproducible.
    LIVE = "LIVE"


class LabRunState(StrEnum):
    """Durable progress of one requested scenario run."""

    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    # No stimulus remains active while paused. Resume deliberately starts the
    # authored schedule again from its beginning so its evidence windows stay
    # valid; it never guesses where a half-applied fault should continue.
    PAUSED = "PAUSED"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    STOPPED = "STOPPED"
    # The request was well-formed but this deployment will not carry it out -
    # no runner is attached, or the scenario is not one it may fire.
    REFUSED = "REFUSED"


class LabRunControl(StrEnum):
    """The three operator intents accepted for an authored run."""

    PAUSE = "PAUSE"
    RESUME = "RESUME"
    STOP = "STOP"


class LabRunControlRequest(ContractModel):
    """Everything a browser may say when controlling one server-owned run."""

    control: LabRunControl

    @field_validator("control", mode="before")
    @classmethod
    def parse_wire_control(cls, value: object) -> object:
        if isinstance(value, str):
            try:
                return LabRunControl(value)
            except ValueError:
                return value
        return value


class LabRunHonesty(ContractModel):
    """What was real about this run, stated rather than implied.

    Both modes drive REAL telemetry through the platform; they differ in where
    that telemetry came from and whether the faults in it were injected. A
    launcher that showed the two identically would be inviting the audience to
    read a recording as a live system.
    """

    telemetry: HumanText
    stimulus: HumanText
    reproducibility: HumanText


class LabRunnerHeartbeat(ContractModel):
    """Recent proof that the separate lab-side process is actually present."""

    worker_id: Identifier
    seen_at: UtcDatetime
    live_ready: bool
    detail: HumanText


class LabScenarioRequest(ContractModel):
    """Everything a client is allowed to say about a run it wants."""

    scenario_id: Identifier
    mode: LabRunMode

    @field_validator("mode", mode="before")
    @classmethod
    def parse_wire_mode(cls, value: object) -> object:
        """FastAPI decodes JSON before strict Pydantic validation."""
        if isinstance(value, str):
            try:
                return LabRunMode(value)
            except ValueError:
                return value
        return value


class LabRunSnapshot(ContractModel):
    """The authoritative state of one requested run."""

    run_id: Identifier
    scenario_id: Identifier
    mode: LabRunMode
    state: LabRunState
    requested_at: UtcDatetime
    started_at: UtcDatetime | None = None
    finished_at: UtcDatetime | None = None
    # Which incident the run produced, when it produced exactly one worth
    # pointing at. Absent is a first-class answer: a live run can be perfectly
    # successful and produce several, or none.
    incident_id: Identifier | None = None
    # Durable intent, consumed by the lab-side runner. The gateway records it
    # and cannot execute it, preserving the same boundary as scenario firing.
    control_requested: LabRunControl | None = None
    # Replay presentation stays in the capture's event-time coordinates. The
    # browser reveals only through the durable cursor; it never retimestamps a
    # recording to make old telemetry look live.
    evidence_start_at: UtcDatetime | None = None
    evidence_end_at: UtcDatetime | None = None
    evidence_cursor_at: UtcDatetime | None = None
    progress: float | None = Field(default=None, ge=0.0, le=1.0)
    detail: HumanText
    honesty: LabRunHonesty

    @model_validator(mode="after")
    def validate_progress(self) -> Self:
        terminal = self.state in {
            LabRunState.SUCCEEDED,
            LabRunState.FAILED,
            LabRunState.STOPPED,
            LabRunState.REFUSED,
        }
        if (self.finished_at is not None) != terminal:
            raise ValueError("a finished time and a terminal state travel together")
        if self.state is LabRunState.QUEUED and self.started_at is not None:
            raise ValueError("a queued run has not started")
        if self.state is LabRunState.RUNNING and self.started_at is None:
            raise ValueError("a running run must say when it started")
        if self.state is LabRunState.PAUSED and self.started_at is None:
            raise ValueError("a paused run must say when it first started")
        if self.control_requested is not None and (
            self.state is not LabRunState.RUNNING
            or self.control_requested not in {LabRunControl.PAUSE, LabRunControl.STOP}
        ):
            raise ValueError("only a running run may carry a pending pause or stop")
        evidence = (self.evidence_start_at, self.evidence_end_at, self.evidence_cursor_at)
        if any(item is not None for item in evidence):
            if any(item is None for item in evidence):
                raise ValueError("replay evidence bounds and cursor travel together")
            if self.mode is not LabRunMode.REPLAY:
                raise ValueError("only a replay may carry capture event-time progress")
            start = self.evidence_start_at
            end = self.evidence_end_at
            cursor = self.evidence_cursor_at
            assert start is not None and end is not None and cursor is not None
            if not start <= cursor <= end:
                raise ValueError("replay evidence cursor must stay within its capture bounds")
        if self.progress is not None and self.state not in {
            LabRunState.RUNNING,
            LabRunState.PAUSED,
            LabRunState.SUCCEEDED,
        }:
            raise ValueError("only an executing, paused or succeeded run carries progress")
        if self.started_at is not None and self.started_at < self.requested_at:
            raise ValueError("a run cannot start before it was requested")
        if self.finished_at is not None:
            began = self.started_at or self.requested_at
            if self.finished_at < began:
                raise ValueError("a run cannot finish before it began")
        if self.incident_id is not None and self.state is not LabRunState.SUCCEEDED:
            raise ValueError("only a succeeded run may name the incident it produced")
        return self


class LabRunFeedStatus(StrEnum):
    """Whether the launcher can be used at all right now."""

    READY = "READY"
    # Nothing is wrong; something else is running and a second run would make
    # both meaningless.
    BUSY = "BUSY"
    UNAVAILABLE = "UNAVAILABLE"


class LabScenarioOption(ContractModel):
    """One scenario a client may fire, and the modes it may fire it in."""

    scenario_id: Identifier
    name: HumanText
    description: HumanText
    modes: tuple[LabRunMode, ...] = Field(min_length=1)
    # Roughly how long the live mode takes, so a button that costs seventeen
    # minutes does not look like one that costs none.
    live_duration_seconds: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def validate_modes(self) -> Self:
        if len(set(self.modes)) != len(self.modes):
            raise ValueError("a scenario lists each mode once")
        if (LabRunMode.LIVE in self.modes) != (self.live_duration_seconds is not None):
            raise ValueError("a live-capable scenario states how long live takes, and only then")
        return self


class LabRunFeed(ContractModel):
    """The launcher's whole view: what may be fired, and what has been."""

    status: LabRunFeedStatus
    scenarios: tuple[LabScenarioOption, ...]
    runs: tuple[LabRunSnapshot, ...]
    # A launcher whose runner is not attached must say so rather than queueing
    # work nothing will ever claim.
    runner_attached: bool
    live_ready: bool
    runner_detail: HumanText
    note: HumanText

    @model_validator(mode="after")
    def validate_runner_readiness(self) -> Self:
        if self.live_ready and not self.runner_attached:
            raise ValueError("a missing runner cannot be live-ready")
        return self
