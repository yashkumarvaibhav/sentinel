"""The security axis: is something deforming behavior rather than adding volume?"""

from __future__ import annotations

from contracts import EvidenceAxis
from decision.agents.base import EvidenceAgent


class SecurityEvidenceAgent(EvidenceAgent):
    """Score hostility from behavioral deformation and unexplained residual volume.

    An event multiplies request rates but preserves behavioral ratios, so this
    agent's claimed evidence is exactly the part a legitimate surge cannot
    produce: deformed source/path entropy, auth-failure and amplification
    ratios, machine-regular arrivals, and the residual an explained band could
    not account for. It reads no reliability signal at all — a hostile actor who
    keeps latency clean stays visible here.
    """

    axis = EvidenceAxis.SECURITY
