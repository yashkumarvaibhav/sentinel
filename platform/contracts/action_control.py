"""Public, intent-only control state for one evidence-owned action plan.

The browser is allowed to say only what an operator intends to do with a plan
revision the server already owns. It cannot submit an action target, rung,
blast radius, TTL, gate result, revert token, or outcome. Those facts are
materialized while the decision evidence and action-plane objects still exist,
then carried forward unchanged through this contract.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal, Self

from pydantic import Field, field_validator, model_validator

from contracts._base import ContractModel, HumanText, Identifier, Probability, UtcDatetime
from contracts.action import (
    DESTRUCTIVE_ACTIONS,
    ActionKind,
    ActionOutcome,
    ActionParameterValue,
    ActionPlan,
    ActionStatus,
    ActuatorKind,
)


class ActionControlIntent(StrEnum):
    """The complete mutation vocabulary exposed to a client."""

    APPROVE = "APPROVE"
    REJECT = "REJECT"
    ROLLBACK = "ROLLBACK"


class ActionControlState(StrEnum):
    """Durable progress of one immutable plan revision."""

    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    APPLY_REQUESTED = "APPLY_REQUESTED"
    REJECTED = "REJECTED"
    APPLIED = "APPLIED"
    VERIFIED = "VERIFIED"
    FAILED = "FAILED"
    ROLLBACK_REQUESTED = "ROLLBACK_REQUESTED"
    ROLLED_BACK = "ROLLED_BACK"
    REFUSED = "REFUSED"
    SIMULATED = "SIMULATED"


class ActionGateStatus(StrEnum):
    """Whether one deterministic safety gate admitted the exact plan."""

    PASSED = "PASSED"
    REFUSED = "REFUSED"


class ActionSloSampleStatus(StrEnum):
    """Whether both signals needed for one SLO judgement were observable."""

    MEASURED = "MEASURED"
    INSUFFICIENT = "INSUFFICIENT"


class RollbackVerificationStatus(StrEnum):
    """What delayed protected-service telemetry can support after a revert."""

    VERIFIED = "VERIFIED"
    FAILED = "FAILED"
    INSUFFICIENT = "INSUFFICIENT"


class ActionControlRequest(ContractModel):
    """An operator's intent against one stable, server-held plan revision."""

    incident_id: Identifier
    plan_revision: int = Field(ge=1)
    intent: ActionControlIntent

    @field_validator("intent", mode="before")
    @classmethod
    def parse_wire_intent(cls, value: object) -> object:
        """FastAPI decodes JSON before strict Pydantic validation."""
        if isinstance(value, str):
            try:
                return ActionControlIntent(value)
            except ValueError:
                return value
        return value


class ActionRungSnapshot(ContractModel):
    """The exact committed ladder choice from which the plan was built."""

    rung_id: Identifier
    ladder_id: Identifier
    actuator: ActuatorKind
    action_kind: ActionKind
    parameters: dict[Identifier, ActionParameterValue] = Field(default_factory=dict)
    ttl_seconds: int = Field(ge=1)
    requires_human_approval: bool
    required_approval_count: int = Field(ge=0, le=2)
    maximum_blast_fraction: Probability
    reason: HumanText
    canary_parameter: Identifier | None = None
    canary_shares: tuple[int, ...] = ()

    @model_validator(mode="after")
    def validate_approval_and_canary(self) -> Self:
        expected_approvals = (
            2 if self.action_kind in DESTRUCTIVE_ACTIONS else int(self.requires_human_approval)
        )
        if self.required_approval_count != expected_approvals:
            raise ValueError(
                "required_approval_count must come from the plan and destructive-rung policy"
            )
        if bool(self.canary_parameter) != bool(self.canary_shares):
            raise ValueError("a canary parameter and its committed shares must travel together")
        if any(not 1 <= share <= 100 for share in self.canary_shares):
            raise ValueError("canary shares must be whole percentages within [1, 100]")
        if tuple(sorted(set(self.canary_shares))) != self.canary_shares:
            raise ValueError("canary shares must be unique and strictly increasing")
        return self


class ActionGateResult(ContractModel):
    """One measured deterministic guard result for the exact plan."""

    gate_id: Identifier
    status: ActionGateStatus
    detail: HumanText


class ActionApproval(ContractModel):
    """One server-authenticated identity approving one plan revision once."""

    actor: Identifier
    approved_at: UtcDatetime


class ActionSloSample(ContractModel):
    """One immutable SLO reading with the exact targets used to judge it."""

    service: Identifier
    sampled_at: UtcDatetime
    status: ActionSloSampleStatus
    availability: Probability | None
    latency_p95_ms: float | None = Field(ge=0.0, allow_inf_nan=False)
    availability_target: Probability
    latency_p95_target_ms: float = Field(gt=0.0, allow_inf_nan=False)

    @model_validator(mode="after")
    def validate_measurement(self) -> Self:
        measured = self.availability is not None and self.latency_p95_ms is not None
        if measured != (self.status is ActionSloSampleStatus.MEASURED):
            raise ValueError(
                "a measured SLO sample carries both signals; insufficient carries neither"
            )
        if self.availability_target == 0.0:
            raise ValueError("an SLO availability target must be greater than zero")
        return self

    @property
    def breaches(self) -> tuple[str, ...]:
        """The committed signals this measured sample misses."""
        if self.status is ActionSloSampleStatus.INSUFFICIENT:
            return ()
        assert self.availability is not None
        assert self.latency_p95_ms is not None
        missed: list[str] = []
        if self.availability < self.availability_target:
            missed.append("availability")
        if self.latency_p95_ms > self.latency_p95_target_ms:
            missed.append("latency_p95_ms")
        return tuple(missed)


class ActionRollbackVerification(ContractModel):
    """Delayed, read-only proof of protected-service recovery after rollback."""

    status: RollbackVerificationStatus
    verified_at: UtcDatetime
    checked_signals: tuple[Literal["availability", "latency_p95_ms"], ...] = Field(
        min_length=2,
        max_length=2,
    )
    before: tuple[ActionSloSample, ...] = Field(min_length=1)
    after: tuple[ActionSloSample, ...] = Field(min_length=1)
    users_restored: Probability | None
    detail: HumanText

    @model_validator(mode="after")
    def validate_evidence(self) -> Self:
        if self.checked_signals != ("availability", "latency_p95_ms"):
            raise ValueError("rollback verification checks availability and latency in that order")
        before_services = tuple(sample.service for sample in self.before)
        after_services = tuple(sample.service for sample in self.after)
        if len(before_services) != len(set(before_services)):
            raise ValueError("rollback verification samples each protected service once")
        if before_services != after_services:
            raise ValueError("rollback verification must compare the same services in stable order")
        for before, after in zip(self.before, self.after, strict=True):
            if (
                before.availability_target != after.availability_target
                or before.latency_p95_target_ms != after.latency_p95_target_ms
            ):
                raise ValueError("rollback verification must use one committed target before/after")
            if before.sampled_at > after.sampled_at or after.sampled_at > self.verified_at:
                raise ValueError("rollback verification evidence cannot move time backwards")
        insufficient = any(
            sample.status is ActionSloSampleStatus.INSUFFICIENT
            for sample in (*self.before, *self.after)
        )
        if insufficient != (self.status is RollbackVerificationStatus.INSUFFICIENT):
            raise ValueError("missing SLO telemetry must produce an insufficient verification")
        if self.status is RollbackVerificationStatus.INSUFFICIENT:
            if self.users_restored is not None:
                raise ValueError("insufficient telemetry cannot claim users_restored")
            return self
        if self.users_restored is None:
            raise ValueError("a measured rollback verification must record users_restored")
        after_breaches = any(sample.breaches for sample in self.after)
        if after_breaches == (self.status is RollbackVerificationStatus.VERIFIED):
            raise ValueError("verified means every measured protected SLO recovered")
        return self


class ActionCanaryStep(ContractModel):
    """One durably recorded widening of a canaried effect.

    A canary walks a dial upward - 5%, then 10%, then all of it - looking at the
    protected signals between steps. Each share is a genuinely different state
    of the world with its own idempotency key, so each one is recorded here as
    it lands rather than being reconstructed afterwards from the share that
    happened to be reached last. That is the whole point: a worker that died
    between two steps must leave behind the exact set of effects that are
    standing, because a revert has to unwind every one of them.
    """

    share: int = Field(ge=1, le=100)
    plan: ActionPlan
    outcome: ActionOutcome
    collateral_clean: bool
    collateral_detail: HumanText
    harmed: tuple[Identifier, ...] = ()

    @model_validator(mode="after")
    def validate_step(self) -> Self:
        if self.outcome.plan_id != self.plan.plan_id:
            raise ValueError("a canary step's outcome must belong to that step's own plan")
        if self.outcome.idempotency_key != self.plan.idempotency_key:
            raise ValueError("a canary step's outcome must carry that step's own effect key")
        if self.collateral_clean and self.harmed:
            raise ValueError("a clean collateral report cannot also name a harmed signal")
        if not self.collateral_clean and not self.harmed:
            # A canary that stops without naming the signal it stopped on is an
            # outage nobody can diagnose.
            raise ValueError("a canary step that found harm must name what was harmed")
        if self.outcome.in_force and self.plan.reversible and self.outcome.revert_token is None:
            raise ValueError("a reversible canary step in force must retain its revert token")
        return self


class ActionControlSnapshot(ContractModel):
    """Authoritative durable control state returned after every mutation."""

    incident_id: Identifier
    plan_revision: int = Field(ge=1)
    state: ActionControlState
    rung: ActionRungSnapshot
    plan: ActionPlan
    guard_results: tuple[ActionGateResult, ...]
    latest_outcome: ActionOutcome | None
    canary_progress: tuple[ActionCanaryStep, ...] = ()
    rollback_slo_before: tuple[ActionSloSample, ...] = ()
    rollback_verification: ActionRollbackVerification | None = None
    approvals: tuple[ActionApproval, ...] = ()
    rejected_by: Identifier | None = None
    rejected_at: UtcDatetime | None = None
    created_at: UtcDatetime
    updated_at: UtcDatetime

    @model_validator(mode="after")
    def validate_snapshot(self) -> Self:
        if self.updated_at < self.created_at:
            raise ValueError("updated_at must be greater than or equal to created_at")
        if self.plan.incident_id != self.incident_id:
            raise ValueError("the control and its server-held plan must name the same incident")
        if (
            self.rung.actuator != self.plan.actuator
            or self.rung.action_kind != self.plan.action_kind
            or dict(self.rung.parameters) != self.plan.parameters
            or self.rung.requires_human_approval != self.plan.requires_human_approval
        ):
            raise ValueError("rung and plan safety-critical fields must agree exactly")
        if (
            self.state is not ActionControlState.REFUSED
            and self.plan.estimated_blast_fraction > self.rung.maximum_blast_fraction
        ):
            raise ValueError("the stored plan exceeds its server-held rung blast-radius ceiling")
        gate_ids = tuple(result.gate_id for result in self.guard_results)
        if len(gate_ids) != len(set(gate_ids)):
            raise ValueError("guard_results must not contain duplicate gate ids")
        refused_gates = tuple(
            result for result in self.guard_results if result.status is ActionGateStatus.REFUSED
        )
        if (self.state is ActionControlState.REFUSED) != bool(refused_gates):
            raise ValueError(
                "a guard refusal state and a refused guard result must travel together"
            )
        approval_actors = tuple(approval.actor for approval in self.approvals)
        if len(approval_actors) != len(set(approval_actors)):
            raise ValueError("an identity may approve one plan revision only once")
        if any(approval.approved_at < self.created_at for approval in self.approvals):
            raise ValueError("an approval cannot predate the plan revision")
        if self.state is ActionControlState.AWAITING_APPROVAL and self.approvals:
            raise ValueError("an awaiting plan cannot already carry an approval")
        if (
            self.state is ActionControlState.APPLY_REQUESTED
            and len(self.approvals) < self.rung.required_approval_count
        ):
            raise ValueError("apply can be requested only after every required approval is durable")
        self._validate_rejection()
        self._validate_canary_progress()
        self._validate_outcome()
        self._validate_rollback_verification()
        return self

    def _validate_canary_progress(self) -> None:
        """Progress must be a real prefix of the shares the operator committed.

        The shares are not free-form history. They are the ladder's own list,
        walked in order, and a recorded step that is not the next one the rung
        names is either a stale payload or an effect nobody authorised. Both are
        refused here, where the claim cannot be quietly repaired downstream.
        """
        if not self.canary_progress:
            return
        if not self.rung.canary_shares:
            raise ValueError("only a rung that names canary shares can record canary progress")
        walked = tuple(step.share for step in self.canary_progress)
        committed = tuple(self.rung.canary_shares)
        if walked != committed[: len(walked)]:
            raise ValueError(
                "canary progress must be the committed shares walked in order, "
                f"got {walked} against {committed}"
            )
        for step in self.canary_progress:
            if step.plan.incident_id != self.incident_id:
                raise ValueError("a canary step must belong to the control's own incident")
            if (
                step.plan.actuator != self.plan.actuator
                or step.plan.action_kind != self.plan.action_kind
                or step.plan.target_ref != self.plan.target_ref
            ):
                raise ValueError("a canary step must widen the control's own effect, not another")
        # The last share equals the rung's own parameter value, so a completed
        # canary really is the plan the control holds - which is what lets that
        # final step's outcome be the control's outcome at all.
        if len(walked) == len(committed):
            final = self.canary_progress[-1]
            if final.plan.plan_id != self.plan.plan_id:
                raise ValueError(
                    "a completed canary's last step must be the control's own server-held plan"
                )

    def _validate_rejection(self) -> None:
        rejected = self.state is ActionControlState.REJECTED
        if rejected != (self.rejected_by is not None and self.rejected_at is not None):
            raise ValueError("only a rejected plan carries both rejection identity and time")
        if self.rejected_at is not None and self.rejected_at < self.created_at:
            raise ValueError("a rejection cannot predate the plan revision")

    def _validate_outcome(self) -> None:
        if self.latest_outcome is not None:
            if self.latest_outcome.plan_id != self.plan.plan_id:
                raise ValueError("latest_outcome must belong to the server-held plan")
            if self.latest_outcome.idempotency_key != self.plan.idempotency_key:
                raise ValueError("latest_outcome must carry the server-held plan's effect key")
            if (
                self.latest_outcome.in_force
                and self.plan.reversible
                and self.latest_outcome.revert_token is None
            ):
                raise ValueError(
                    "a reversible effect in force must retain its server-held revert token"
                )
            if self.latest_outcome.in_force and tuple(self.latest_outcome.approvals) != tuple(
                approval.actor for approval in self.approvals
            ):
                raise ValueError(
                    "an in-force outcome must retain the control's authenticated approvals"
                )
        expected = {
            ActionControlState.APPLIED: ActionStatus.APPLIED,
            ActionControlState.VERIFIED: ActionStatus.VERIFIED,
            ActionControlState.FAILED: ActionStatus.FAILED,
            ActionControlState.ROLLED_BACK: ActionStatus.REVERTED,
            ActionControlState.SIMULATED: ActionStatus.SIMULATED,
        }
        if self.state in expected:
            if (
                self.latest_outcome is None
                or self.latest_outcome.status is not expected[self.state]
            ):
                # A canary that stopped short never applied the control's own
                # plan, so there is no outcome of that plan to carry - and one
                # is not invented to fill the slot. What proves it is undone is
                # that every share it did apply has been put back.
                if self.state is ActionControlState.ROLLED_BACK and self._canary_fully_unwound():
                    return
                raise ValueError(f"{self.state.value} requires its matching verified outcome")
        elif self.state is ActionControlState.ROLLBACK_REQUESTED:
            # A canary that stopped part-way holds no outcome of its own - the
            # share it reached is not the rung's own value - but the effect it
            # applied is standing in production all the same, and that is
            # precisely what needs undoing.
            standing = any(step.outcome.in_force for step in self.canary_progress)
            held = self.latest_outcome is not None and self.latest_outcome.in_force
            if not (held or standing):
                raise ValueError("rollback can be requested only for a server-held effect in force")
        elif self.latest_outcome is not None:
            raise ValueError(f"{self.state.value} cannot carry an action outcome")

    def _canary_fully_unwound(self) -> bool:
        """Whether a stopped canary applied shares and none of them still stands."""
        if not self.canary_progress:
            return False
        return all(not step.outcome.in_force for step in self.canary_progress)

    def _validate_rollback_verification(self) -> None:
        if self.rollback_slo_before:
            services = tuple(sample.service for sample in self.rollback_slo_before)
            if len(services) != len(set(services)):
                raise ValueError("rollback_slo_before samples each protected service once")
        if self.state is not ActionControlState.ROLLED_BACK:
            if self.rollback_slo_before or self.rollback_verification is not None:
                raise ValueError("only a rolled-back control carries rollback SLO evidence")
            return
        if self.rollback_verification is not None:
            if tuple(self.rollback_verification.before) != tuple(self.rollback_slo_before):
                raise ValueError("rollback verification must use the durable pre-revert samples")
            if self.rollback_verification.verified_at < self.updated_at:
                raise ValueError("rollback verification cannot predate the control settlement")


class ActionControlResponse(ContractModel):
    """Typed API response for the latest authoritative plan revision."""

    status: Literal["ready", "not_found", "degraded"]
    control: ActionControlSnapshot | None
    message: HumanText | None

    @model_validator(mode="after")
    def validate_status(self) -> Self:
        ready = self.status == "ready"
        if ready != (self.control is not None and self.message is None):
            raise ValueError("ready requires a control; unavailable states require a message")
        return self
