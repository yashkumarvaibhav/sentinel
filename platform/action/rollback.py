"""SLO-anchored rollback: measure who was harmed, and measure who came back.

The claim this module exists to make honestly is ``users_restored``. It is the
most quotable number an autonomous remediator produces and the easiest one to
fake, so it is computed here from two readings of the same committed SLO - one
taken while the action was in force and one taken after it was reverted - or it
is not produced at all.

Three rules follow from that:

* **A reading that is missing is not a reading that is fine.** A service whose
  SLO could not be read is reported as *unknown*, never as healthy. An action
  cannot be declared collateral-free because the telemetry was down.
* **Harm is measured against the operator's own target**, not against a delta
  invented here: ``config/slo.yml`` states the availability and latency each
  service is held to, and that file is inside the fingerprinted runtime bundle,
  so it is read and never written by this plane.
* **A rollback that cannot be verified still happens.** The revert is the safe
  direction; the measurement is how we describe it afterwards. Refusing to undo
  something because we could not measure the undo would be exactly backwards.

``SloCollateralProbe`` is the concrete ``CollateralProbe`` the canary in
``action/guards.py`` was left abstract for: 5.6 deliberately did not guess what
collateral meant, and this is the answer.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from action.executor import ActionExecutor
from action.guards import CollateralReport
from common.config import ServiceSlo, SloConfig
from contracts import ActionOutcome, ActionPlan


@dataclass(frozen=True, slots=True)
class SloReading:
    """One measurement of a service against the target it is held to."""

    service: str
    availability: float
    latency_p95_ms: float

    def breaches(self, slo: ServiceSlo) -> tuple[str, ...]:
        """Which of this service's committed targets the reading misses."""
        missed: list[str] = []
        if self.availability < slo.availability_target:
            missed.append(
                f"availability {self.availability:.4f} below target {slo.availability_target:.4f}"
            )
        if self.latency_p95_ms > slo.latency_p95_ms:
            missed.append(
                f"p95 latency {self.latency_p95_ms:.0f}ms above target {slo.latency_p95_ms:.0f}ms"
            )
        return tuple(missed)


class SloReader(Protocol):
    """Reads a service's current SLO position, or reports that it could not.

    Returning ``None`` is a first-class answer and the reason this is a Protocol
    rather than a function: "we could not see" and "it is fine" must not be the
    same value, and the store this eventually reads from is a Phase-8 concern.
    """

    def __call__(self, service: str, *, ts: datetime) -> SloReading | None:
        """The service's position against its SLO now, or None if unreadable."""


@dataclass(frozen=True, slots=True)
class SloCollateralProbe:
    """Reports whether an action in force is costing a protected service its SLO.

    The services watched are the ones ``config/slo.yml`` names, **not** the one
    the action targets: collateral is by definition what happens somewhere the
    action was not aimed at.
    """

    slos: SloConfig
    reader: SloReader

    def __call__(self, plan: ActionPlan, *, ts: datetime) -> CollateralReport:
        harmed: list[str] = []
        unreadable: list[str] = []
        for slo in self.slos.slos:
            reading = self.reader(slo.service, ts=ts)
            if reading is None:
                unreadable.append(slo.service)
                continue
            if reading.breaches(slo):
                harmed.append(slo.service)
        if unreadable:
            # Not harmed, and not clean either. An action must not widen because
            # the telemetry that would have stopped it was unavailable.
            return CollateralReport(
                clean=False,
                detail=(
                    f"could not read the SLO position of {', '.join(sorted(unreadable))}; a "
                    "protected service we cannot see is not a protected service we know is fine"
                ),
                harmed=tuple(sorted(unreadable)),
            )
        if harmed:
            return CollateralReport(
                clean=False,
                detail=f"{', '.join(sorted(harmed))} fell below its committed SLO while "
                f"{plan.plan_id} was in force",
                harmed=tuple(sorted(harmed)),
            )
        return CollateralReport(
            clean=True,
            detail=f"every service in slo.yml held its target while {plan.plan_id} was in force",
        )


@dataclass(frozen=True, slots=True)
class RollbackResult:
    """What a rollback found, did, and could honestly claim afterwards."""

    harmed: tuple[str, ...]
    reverted: bool
    outcome: ActionOutcome | None
    detail: str
    # The measured recovery in availability on the worst-harmed service: what it
    # read after the revert minus what it read while the action was in force.
    # None when either reading is missing - an unmeasured recovery is not a
    # smaller recovery, it is one nobody may quote.
    availability_restored: float | None = None

    @property
    def users_restored(self) -> float | None:
        """The share of requests that stopped failing once the action was undone."""
        return self.availability_restored


class VerifiedRollback:
    """Undoes an action that cost a protected service its SLO, and measures the undo."""

    __slots__ = ("_executor", "_reader", "_slos")

    def __init__(self, *, executor: ActionExecutor, slos: SloConfig, reader: SloReader) -> None:
        self._executor = executor
        self._slos = slos
        self._reader = reader

    def probe(self) -> SloCollateralProbe:
        """The collateral probe this rollback's own judgement is based on."""
        return SloCollateralProbe(slos=self._slos, reader=self._reader)

    def rollback_if_harmed(
        self,
        plan: ActionPlan,
        *,
        ts: datetime,
        owner: str,
        approvals: Sequence[str] = (),
        settled_at: datetime | None = None,
    ) -> RollbackResult:
        """Read the SLOs, undo the action if anything protected is suffering, read again.

        ``settled_at`` is when to take the second reading. It is passed in rather
        than derived from a clock so a replay measures a recovery exactly as a
        live run does.
        """
        report = self.probe()(plan, ts=ts)
        if report.clean:
            return RollbackResult(
                harmed=(),
                reverted=False,
                outcome=None,
                detail=report.detail,
            )
        during = self._worst(report.harmed, ts=ts)
        outcome = self._executor.revert(plan, ts=ts, owner=owner, approvals=approvals)
        after = self._worst(report.harmed, ts=settled_at or ts)
        restored = None if during is None or after is None else after - during
        return RollbackResult(
            harmed=report.harmed,
            reverted=True,
            outcome=outcome,
            detail=(
                f"{plan.plan_id} was undone because {report.detail}"
                + (
                    f"; availability on the worst-affected service recovered by {restored:+.4f}"
                    if restored is not None
                    else "; the recovery could not be measured, so none is claimed"
                )
            ),
            availability_restored=restored,
        )

    def _worst(self, services: Sequence[str], *, ts: datetime) -> float | None:
        """The lowest availability across the harmed services, or None if unreadable.

        The worst one is the honest anchor: a rollback's effect should be quoted
        against the service that was suffering most, not averaged into comfort.
        """
        readings = [self._reader(service, ts=ts) for service in services]
        measured = [reading.availability for reading in readings if reading is not None]
        if not measured or len(measured) != len(readings):
            return None
        return min(measured)
