"""The business-impact axis: how many people are actually being hurt right now?"""

from __future__ import annotations

from collections.abc import Mapping

from contracts import EvidenceAxis, SymptomEpisode
from decision.agents.base import EvidenceAgent
from decision.config import CriticalityWeightsConfig, EvidenceAxisConfig


class BusinessImpactEvidenceAgent(EvidenceAgent):
    """Score user-visible harm, weighted by how critical the hurting service is.

    The fan-impact proxy is deliberately simple and honest: it claims only the
    symptoms a user would feel - conversion collapsing, request volume
    dropping, a stream going silent - and scales each by the topology
    criticality of the service it happened on, so the same episode on the
    checkout edge outweighs one on a background worker.

    Criticality comes from committed topology, not from another agent, so a
    quiet security axis can never make impact look smaller than it is.
    """

    axis = EvidenceAxis.BUSINESS_IMPACT

    def __init__(
        self,
        *,
        configuration: EvidenceAxisConfig,
        criticality: Mapping[str, str],
    ) -> None:
        super().__init__(configuration=configuration)
        weights = configuration.criticality_weights
        if weights is None:  # defensive: config validation already requires it
            raise ValueError("BUSINESS_IMPACT configuration must carry criticality_weights")
        if not criticality:
            raise ValueError("business impact needs the topology criticality of each service")
        self._weights: CriticalityWeightsConfig = weights
        # Resolve every service once so an unknown criticality fails at
        # construction rather than silently mid-incident.
        self._scaling = {
            service: weights.weight_for(level) for service, level in criticality.items()
        }

    def _episode_multiplier(self, episode: SymptomEpisode) -> float:
        """Scale a user-visible symptom by how critical its service is.

        A service missing from committed topology contributes nothing: the
        agent refuses to guess how much an unknown service matters to users.
        """
        return self._scaling.get(episode.service, 0.0)
