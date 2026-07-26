"""The policy gate: a verified incident becomes a decision the platform may act on.

Everything upstream of here only ever *described* the world. This is the module
that says what happens about it, and it is the last thing standing between a
hypothesis and production, so its structure is deliberately dull:

1. **Rules propose.** An ordered, operator-owned table is walked top down and
   the first rule whose stated requirements hold names an action. The table is
   required to end in a catch-all, because unlike fusion - which may honestly
   refuse to name a diagnosis - a gate that declines to decide leaves an
   incident with no stated handling at all.
2. **Guards restrict.** A guard may only replace an *acting* action with a
   non-acting fallback. Each guard is a fact computed here from telemetry,
   committed topology or the verification: was the hypothesis confirmed, did
   the collapse name a target, is the incident still live, is the confidence
   high enough, is the blast radius small enough. No model output reaches them.
3. **Floors raise.** A floor may only pull a gate that would otherwise stay
   quiet up to ``ALERT`` or ``ESCALATE_TO_HUMAN``; the configuration validator
   rejects a floor that names an acting rung. Guards and floors therefore
   cannot fight: guards remove authority, floors restore attention, and every
   acting rung already outranks ``ALERT`` on the attention ladder.
4. **Windows override.** A change freeze or maintenance window applies last,
   because it is the one input where a named person has taken responsibility -
   with an owner, a reason and an expiry recorded on the decision.

Two things are worth stating plainly.

**The decision is taken on the incident's peak evidence.** A storm that has
gone quiet for one tick is still the storm, and the tick-wide verdict attached
to a still-open incident may be reading the calm rather than the problem. The
gate therefore remembers the strongest verdict and the worst severity seen
while the incident was unresolved, and records the moment that evidence was
measured. The *verification*, by contrast, is always the current one: stale
confirmation is not confirmation. Each input is read at whichever tick is the
more conservative.

**Nothing here can invent authority.** ``target_service`` is the collapsed
origin and nothing else; ``requires_human_approval`` is derived from the
reasons computed for it; and the ``Decision`` contract independently refuses an
acting decision that is unconfirmed, untargeted or unbacked by a verdict. The
policy can narrow what the platform may do. It cannot widen it.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from common.config import TopologyConfig
from contracts import (
    ACTING_ACTIONS,
    ESCALATING_ACTIONS,
    AppliedSuppression,
    CheckOutcome,
    Decision,
    DecisionAction,
    FusionStatus,
    Incident,
    IncidentSeverity,
    IncidentState,
    SuppressionKind,
    Verdict,
    Verification,
)
from decision.config import (
    ActionFloorConfig,
    ActionPolicyConfig,
    ActionRuleConfig,
    SuppressionWindowConfig,
)

# Worst last: the order a responder reads these in, used to keep an incident's
# peak severity rather than whatever the latest tick measured.
SEVERITY_ORDER: tuple[IncidentSeverity, ...] = (
    IncidentSeverity.LOW,
    IncidentSeverity.MEDIUM,
    IncidentSeverity.HIGH,
    IncidentSeverity.CRITICAL,
)

# How much attention each rung claims. Floors compare on this and nothing else,
# so raising a floor can never take containment away: every rung that touches
# production or pages a person already outranks ALERT.
_ATTENTION: Mapping[DecisionAction, int] = {
    DecisionAction.SUPPRESS: 0,
    DecisionAction.ALERT: 1,
    DecisionAction.ACT: 2,
    DecisionAction.ESCALATE_TO_HUMAN: 2,
    DecisionAction.AUTO_CONTAIN_THEN_ESCALATE: 2,
}

# The states in which an incident is still happening. Acting on anything else
# remediates a problem that has already stopped.
LIVE_STATES: frozenset[IncidentState] = frozenset({IncidentState.OPEN, IncidentState.MITIGATING})


def severity_rank(severity: IncidentSeverity) -> int:
    """Where one severity sits on the responder's ladder."""
    return SEVERITY_ORDER.index(severity)


@dataclass(frozen=True, slots=True)
class IncidentEvidence:
    """The strongest evidence an incident produced while it was unresolved.

    ``ts`` is the moment that evidence was measured, which is what stops a
    decision presenting a peak from four minutes ago as a current reading.
    """

    ts: datetime
    severity: IncidentSeverity
    verdict: Verdict | None
    fusion_status: FusionStatus

    def strengthened_by(
        self,
        *,
        ts: datetime,
        severity: IncidentSeverity,
        verdict: Verdict | None,
        fusion_status: FusionStatus,
    ) -> IncidentEvidence:
        """Fold one tick's reading into the peak, keeping the stronger of each.

        Severity and diagnosis peak independently: an incident can be widest at
        one tick and best explained at another, and taking the worse of each is
        the conservative reading of both.
        """
        peak_severity = max((self.severity, severity), key=severity_rank)
        keep_verdict = self.verdict is not None and (
            verdict is None or verdict.confidence <= self.verdict.confidence
        )
        if keep_verdict:
            return IncidentEvidence(
                ts=self.ts,
                severity=peak_severity,
                verdict=self.verdict,
                fusion_status=self.fusion_status,
            )
        return IncidentEvidence(
            ts=ts,
            severity=peak_severity,
            verdict=verdict,
            fusion_status=fusion_status,
        )


class PolicyGate:
    """Turn verified incidents into decisions, under one versioned policy.

    The gate is stateful across ticks only because an incident's peak evidence
    is: it holds no clock, performs no I/O and calls no model, so replaying the
    same stream reproduces every decision exactly.
    """

    def __init__(self, *, configuration: ActionPolicyConfig, topology: TopologyConfig) -> None:
        self._configuration = configuration
        self._criticality = {service.service: service.criticality for service in topology.services}
        self._peaks: dict[str, IncidentEvidence] = {}

    @property
    def configuration(self) -> ActionPolicyConfig:
        """The policy every decision from this gate was taken under."""
        return self._configuration

    def peak(self, incident_id: str) -> IncidentEvidence | None:
        """The strongest evidence recorded for one incident, if it has any."""
        return self._peaks.get(incident_id)

    def decide(
        self,
        incident: Incident,
        *,
        ts: datetime,
        verdict: Verdict | None,
        verification: Verification,
        fusion_status: FusionStatus,
    ) -> Decision:
        """Decide what happens about one incident at one tick."""
        moment = _utc(ts)
        if verification.incident_id != incident.incident_id:
            raise ValueError("verification must belong to the incident being decided")
        evidence = self._peak_evidence(
            incident,
            ts=moment,
            verdict=verdict,
            fusion_status=fusion_status,
        )
        rule = self._first_matching_rule(incident, evidence=evidence, verification=verification)
        action = rule.action
        reason = rule.reason
        guards, action, guard_reasons = self._apply_guards(
            action,
            incident=incident,
            evidence=evidence,
            verification=verification,
        )
        floors, action = self._apply_floors(action, incident=incident, evidence=evidence)
        window, action, window_reason = self._apply_windows(
            action,
            incident=incident,
            evidence=evidence,
            ts=moment,
        )
        approvals = self._approval_reasons(
            action,
            incident=incident,
            evidence=evidence,
            verification=verification,
            rule=rule,
            guard_reasons=guard_reasons,
            window=window,
        )
        escalations = self._escalation_reasons(action, rule=rule)
        target = incident.origin_service if action in ACTING_ACTIONS else None
        peak_verdict = evidence.verdict
        stated = _reason(
            reason,
            rule=rule,
            guard_reasons=guard_reasons,
            floors=floors,
            window_reason=window_reason,
        )
        return Decision(
            decision_id=_decision_id(incident.incident_id, moment, action, rule.rule_id),
            ts=moment,
            incident_id=incident.incident_id,
            action=action,
            rule_id=rule.rule_id,
            reason=stated,
            evidence_ts=evidence.ts,
            severity=evidence.severity,
            confirmed=verification.confirmed,
            verification_id=verification.verification_id,
            requires_human_approval=bool(approvals),
            verdict_class=None if peak_verdict is None else peak_verdict.verdict_class,
            verdict_id=None if peak_verdict is None else peak_verdict.verdict_id,
            confidence=None if peak_verdict is None else peak_verdict.confidence,
            target_service=target,
            approval_reasons=approvals,
            escalation_reasons=escalations,
            guards_applied=guards,
            floors_applied=floors,
            suppression=window,
        )

    def _peak_evidence(
        self,
        incident: Incident,
        *,
        ts: datetime,
        verdict: Verdict | None,
        fusion_status: FusionStatus,
    ) -> IncidentEvidence:
        """Fold this tick into the incident's peak, or start the peak from it.

        A resolved incident starts again from what is true now: keeping the
        peak of a problem that is over would page forever.
        """
        reading = IncidentEvidence(
            ts=ts,
            severity=incident.severity,
            verdict=verdict,
            fusion_status=fusion_status,
        )
        if incident.state is IncidentState.RESOLVED:
            self._peaks.pop(incident.incident_id, None)
            return reading
        held = self._peaks.get(incident.incident_id)
        peak = (
            reading
            if held is None
            else held.strengthened_by(
                ts=ts,
                severity=incident.severity,
                verdict=verdict,
                fusion_status=fusion_status,
            )
        )
        self._peaks[incident.incident_id] = peak
        return peak

    def _first_matching_rule(
        self,
        incident: Incident,
        *,
        evidence: IncidentEvidence,
        verification: Verification,
    ) -> ActionRuleConfig:
        """Walk the ladder top down; the validated catch-all guarantees an answer."""
        for rule in self._configuration.rules:
            if _rule_matches(rule, incident=incident, evidence=evidence, verification=verification):
                return rule
        # defensive: the configuration validator requires a catch-all last rule
        raise ValueError("no action rule matched and the table has no catch-all")

    def _apply_guards(
        self,
        action: DecisionAction,
        *,
        incident: Incident,
        evidence: IncidentEvidence,
        verification: Verification,
    ) -> tuple[tuple[str, ...], DecisionAction, tuple[str, ...]]:
        """Strip authority the evidence does not support, in a stated order."""
        if action not in ACTING_ACTIONS:
            return ((), action, ())
        guards = self._configuration.guards
        verdict = evidence.verdict
        blocked: list[tuple[str, str]] = []
        # Not configurable, unlike the guards below it: the contract refuses an
        # action with no diagnosis behind it, so a policy that proposed one
        # would crash rather than be honoured. Refusing it here is the same
        # rule, stated where it can be explained.
        if verdict is None:
            blocked.append(
                (
                    "require_verdict",
                    "no diagnosis was named for this incident, so there is nothing to act on",
                )
            )
        if guards.require_verification and not verification.confirmed:
            failed = ", ".join(
                check.name for check in verification.checks if check.outcome is CheckOutcome.FAILED
            )
            blocked.append(
                (
                    "require_verification",
                    f"the hypothesis is unconfirmed ({failed or 'no check passed'}), "
                    "so nothing autonomous may run against it",
                )
            )
        if guards.require_named_origin and incident.origin_service is None:
            blocked.append(
                (
                    "require_named_origin",
                    "the storm did not collapse to one origin, so there is no target to act on",
                )
            )
        if guards.require_active_incident and incident.state not in LIVE_STATES:
            blocked.append(
                (
                    "require_active_incident",
                    f"the incident is {incident.state.value.lower()} and every symptom has "
                    "closed, so there is nothing left to remediate",
                )
            )
        if verdict is not None and verdict.confidence < guards.minimum_act_confidence:
            blocked.append(
                (
                    "minimum_act_confidence",
                    f"confidence {verdict.confidence:.3f} is below the "
                    f"{guards.minimum_act_confidence:.2f} an autonomous action requires",
                )
            )
        if len(incident.services) > guards.maximum_affected_services:
            blocked.append(
                (
                    "maximum_affected_services",
                    f"{len(incident.services)} services are symptomatic, beyond the "
                    f"{guards.maximum_affected_services} one reversible action should be aimed at",
                )
            )
        if not blocked:
            return ((), action, ())
        return (
            tuple(name for name, _ in blocked),
            guards.fallback_action,
            tuple(detail for _, detail in blocked),
        )

    def _apply_floors(
        self,
        action: DecisionAction,
        *,
        incident: Incident,
        evidence: IncidentEvidence,
    ) -> tuple[tuple[str, ...], DecisionAction]:
        """Raise a gate that would otherwise stay quiet about something measured."""
        applied: list[str] = []
        for floor in self._configuration.floors:
            if not _floor_matches(floor, incident=incident, evidence=evidence):
                continue
            if _ATTENTION[action] >= _ATTENTION[floor.minimum_action]:
                continue
            applied.append(floor.floor_id)
            action = floor.minimum_action
        return (tuple(applied), action)

    def _apply_windows(
        self,
        action: DecisionAction,
        *,
        incident: Incident,
        evidence: IncidentEvidence,
        ts: datetime,
    ) -> tuple[AppliedSuppression | None, DecisionAction, str | None]:
        """Apply the operator's own override last, and record who owns it."""
        services = (*incident.services, *incident.implicated_services)
        verdict = evidence.verdict
        for window in self._configuration.windows_in_force(ts, services=services):
            if window.kind is SuppressionKind.CHANGE_FREEZE:
                if action not in ACTING_ACTIONS:
                    continue
                return (
                    _applied(window),
                    self._configuration.guards.fallback_action,
                    f"change freeze `{window.window_id}` ({window.owner}) forbids autonomous "
                    "action, so this is handed to a person instead",
                )
            if verdict is None or verdict.verdict_class not in window.applies_to_classes:
                continue
            if not set(incident.services) <= set(window.services):
                continue
            capped = window.maximum_action
            if capped is None or _ATTENTION[action] <= _ATTENTION[capped]:
                continue
            return (
                _applied(window),
                capped,
                f"maintenance window `{window.window_id}` ({window.owner}) covers this "
                f"disruption until {window.expires_ts.isoformat()}",
            )
        return (None, action, None)

    def _approval_reasons(
        self,
        action: DecisionAction,
        *,
        incident: Incident,
        evidence: IncidentEvidence,
        verification: Verification,
        rule: ActionRuleConfig,
        guard_reasons: Sequence[str],
        window: AppliedSuppression | None,
    ) -> tuple[str, ...]:
        """Compute, from evidence alone, why a person must SIGN before this happens.

        Only the rungs where there is something to approve carry reasons.
        Suppressing is the absence of a response and alerting IS telling a
        person, so neither is an approvable event; claiming approval for them
        would make the field mean two different things.

        **Routing to a person is not one of these reasons.** That fact lives in
        ``_escalation_reasons``, because the action plane reads this field as a
        consent gate: while "the ladder routes this to a person" was recorded
        here, ``AUTO_CONTAIN_THEN_ESCALATE`` could never contain anything - it
        waited for a signature that the whole point of the rung was to not need.
        """
        if action in (DecisionAction.SUPPRESS, DecisionAction.ALERT):
            return ()
        approval = self._configuration.approval
        reasons: list[str] = []
        reasons.extend(guard_reasons)
        if window is not None and window.kind is SuppressionKind.CHANGE_FREEZE:
            reasons.append(
                f"change freeze `{window.window_id}` is owned by {window.owner} until "
                f"{window.expires_ts.isoformat()}"
            )
        if evidence.severity in approval.severities:
            reasons.append(f"severity {evidence.severity.value} was measured for this incident")
        named = (*incident.services, *incident.implicated_services)
        critical = sorted(
            service
            for service in named
            if self._criticality.get(service) in approval.service_criticalities
        )
        if critical:
            reasons.append(
                f"committed topology calls {', '.join(critical)} critical to the product"
            )
        minimum = approval.minimum_affected_services
        if minimum is not None and len(incident.services) >= minimum:
            reasons.append(
                f"{len(incident.services)} services are symptomatic, which is a storm rather "
                "than a single fault"
            )
        if action in ACTING_ACTIONS and not verification.confirmed:
            # defensive: the guards already block this and the contract refuses it
            reasons.append("the hypothesis behind this action is not confirmed")
        return tuple(dict.fromkeys(reasons))

    def _escalation_reasons(
        self, action: DecisionAction, *, rule: ActionRuleConfig
    ) -> tuple[str, ...]:
        """Why a person is being brought in - which is not why one must consent.

        Kept apart from ``_approval_reasons`` on purpose. Both are true things to
        say about a decision and only one of them is a gate: telling somebody
        what the platform is doing must never stop the platform doing it, and
        that is precisely what happened while these shared a field.
        """
        if action not in ESCALATING_ACTIONS:
            return ()
        return (f"the ladder routes this to a person: {rule.reason}",)


def _rule_matches(
    rule: ActionRuleConfig,
    *,
    incident: Incident,
    evidence: IncidentEvidence,
    verification: Verification,
) -> bool:
    """Whether every requirement a rule states holds for this incident."""
    verdict = evidence.verdict
    if rule.verdict_classes and (
        verdict is None or verdict.verdict_class not in rule.verdict_classes
    ):
        return False
    if rule.fusion_statuses and evidence.fusion_status not in rule.fusion_statuses:
        return False
    if rule.incident_states and incident.state not in rule.incident_states:
        return False
    if rule.requires_confirmation is not None and verification.confirmed != (
        rule.requires_confirmation
    ):
        return False
    if rule.minimum_confidence is not None and (
        verdict is None or verdict.confidence < rule.minimum_confidence
    ):
        return False
    return not (
        rule.minimum_severity is not None
        and severity_rank(evidence.severity) < severity_rank(rule.minimum_severity)
    )


def _floor_matches(
    floor: ActionFloorConfig,
    *,
    incident: Incident,
    evidence: IncidentEvidence,
) -> bool:
    """Whether the measured fact a floor asks about holds for this incident."""
    if floor.incident_states and incident.state not in floor.incident_states:
        return False
    if floor.fusion_statuses and evidence.fusion_status not in floor.fusion_statuses:
        return False
    if floor.minimum_severity is not None and (
        severity_rank(evidence.severity) < severity_rank(floor.minimum_severity)
    ):
        return False
    # An unmeasured impact is not a small one: a floor that asks about impact
    # simply does not apply when nothing measured it.
    return not (
        floor.minimum_business_impact is not None
        and (
            incident.business_impact is None
            or incident.business_impact < floor.minimum_business_impact
        )
    )


def _applied(window: SuppressionWindowConfig) -> AppliedSuppression:
    """Record the window that held a decision back, with its owner and expiry."""
    return AppliedSuppression(
        window_id=window.window_id,
        kind=window.kind,
        owner=window.owner,
        reason=window.reason,
        expires_ts=window.expires_ts,
    )


def _reason(
    proposed: str,
    *,
    rule: ActionRuleConfig,
    guard_reasons: Sequence[str],
    floors: Sequence[str],
    window_reason: str | None,
) -> str:
    """State the decision the way it was actually reached, stage by stage."""
    parts = [proposed]
    parts.extend(guard_reasons)
    if floors:
        parts.append(f"raised by {', '.join(floors)}")
    if window_reason is not None:
        parts.append(window_reason)
    if len(parts) == 1:
        return proposed
    return f"{rule.rule_id}: " + "; ".join(parts)


def _decision_id(
    incident_id: str,
    ts: datetime,
    action: DecisionAction,
    rule_id: str,
) -> str:
    identity = {
        "incident_id": incident_id,
        "ts": ts.isoformat(),
        "action": action.value,
        "rule_id": rule_id,
    }
    rendered = json.dumps(identity, allow_nan=False, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(rendered.encode("utf-8")).hexdigest()


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError("ts must be a datetime")
    if value.utcoffset() != timedelta(0):
        raise ValueError("ts must be timezone-aware UTC")
    return value.astimezone(UTC)
