"""The change/config axis: did we do this to ourselves, and how recently?"""

from __future__ import annotations

from contracts import ChangeEvent, EvidenceAxis, EvidenceDirection, EvidenceItem
from decision.agents.base import (
    CHANGE_COVERAGE_KIND,
    AgentEvidenceWindow,
    Contribution,
    EvidenceAgent,
    order_contributions,
)
from decision.config import ChangePressureConfig, EvidenceAxisConfig

# Any signal satisfies the whole-kind DEPLOY_MARKER claim; naming it keeps the
# lookup readable rather than passing an empty string around.
_MARKER_SIGNAL = "change_feed"


class ChangeConfigEvidenceAgent(EvidenceAgent):
    """Score deploy-correlated pressure from the operator change feed alone.

    Its evidence is the system's own recent history: what the team deployed,
    what the cluster rolled out, which flags moved. Relevance decays linearly to
    nothing at the configured correlation horizon, so an old deploy stops
    arguing for anything, and a change on a service with no symptom of its own
    keeps only the configured unrelated fraction of its weight.

    The ``DEPLOY_MARKER`` claim's weight is the axis ceiling: correlation in
    time is suggestive, never proof, so even a fresh change on a symptomatic
    service cannot drive this axis to certainty on its own.

    It reads no detector score and no other agent. The only thing it takes from
    the episode stream is which services are currently symptomatic - a fact
    about telemetry, not a conclusion about it.
    """

    axis = EvidenceAxis.CHANGE_CONFIG

    def __init__(self, *, configuration: EvidenceAxisConfig) -> None:
        super().__init__(configuration=configuration)
        pressure = configuration.change_pressure
        if pressure is None:  # defensive: config validation already requires it
            raise ValueError("CHANGE_CONFIG configuration must carry change_pressure settings")
        if configuration.claimed_kinds != {CHANGE_COVERAGE_KIND}:
            raise ValueError(
                f"CHANGE_CONFIG must claim exactly {CHANGE_COVERAGE_KIND.value}, "
                "the coverage marker for the change feed"
            )
        ceiling = configuration.weight_for(CHANGE_COVERAGE_KIND, _MARKER_SIGNAL)
        if ceiling is None:  # defensive: the claim check above already guarantees it
            raise ValueError("CHANGE_CONFIG must weight its change-feed claim")
        self._pressure: ChangePressureConfig = pressure
        self._ceiling = ceiling

    def _contributions(self, window: AgentEvidenceWindow) -> tuple[Contribution, ...]:
        horizon = self._pressure.correlation_window_seconds
        symptomatic = window.symptomatic_services
        found: list[Contribution] = []
        for change in window.changes:
            age = (window.ts - change.ts).total_seconds()
            if age >= horizon:
                continue
            kind_weight = self._pressure.weight_for(change.kind)
            if kind_weight is None:
                continue
            related = change.service in symptomatic
            relevance = 1.0 if related else self._pressure.unrelated_service_factor
            contribution = self._ceiling * kind_weight * (1.0 - age / horizon) * relevance
            if contribution < self._configuration.minimum_contribution:
                continue
            found.append(
                Contribution(
                    service=change.service,
                    item=self._change_item(
                        change,
                        age=age,
                        horizon=horizon,
                        related=related,
                        contribution=min(contribution, 1.0),
                    ),
                )
            )
        return order_contributions(found)

    def _change_item(
        self,
        change: ChangeEvent,
        *,
        age: float,
        horizon: float,
        related: bool,
        contribution: float,
    ) -> EvidenceItem:
        relation = (
            "on a service showing symptoms"
            if related
            else "on a service with no symptom of its own"
        )
        note = (
            f"{change.kind.value} {change.change_id} {relation}: {change.summary}; "
            f"{age:.0f}s before this tick, inside the {horizon:.0f}s correlation window "
            f"({change.source}, {change.honesty})"
        )
        return EvidenceItem(
            feature=f"{change.service}.change.{change.kind.value.lower()}",
            value=age,
            baseline=horizon,
            direction=EvidenceDirection.BELOW_BASELINE,
            contribution=contribution,
            note=note,
            evidence_refs=(change.change_id,),
        )
