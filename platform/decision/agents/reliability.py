"""The reliability axis: is the system itself failing, independent of who is calling?"""

from __future__ import annotations

from contracts import EvidenceAxis
from decision.agents.base import EvidenceAgent


class ReliabilityEvidenceAgent(EvidenceAgent):
    """Score service health from degradation, saturation, error bursts and silence.

    Its claimed evidence is the shape a self-inflicted fault leaves behind:
    dependency edges slowing or erroring, resources climbing into their limit,
    log templates bursting, and streams dropping or going silent. It reads no
    security signal, so a fault stays fully visible during an attack and an
    attack never inflates the fault score.
    """

    axis = EvidenceAxis.RELIABILITY
