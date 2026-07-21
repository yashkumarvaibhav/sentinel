"""Small validated configuration factories shared by detector unit tests."""

from common.config import (
    BehavioralRatioConfig,
    BehavioralRatioRuleConfig,
    LogTemplateConfig,
    SequenceRatioRuleConfig,
)


def behavioral_ratio_config() -> BehavioralRatioConfig:
    return BehavioralRatioConfig(
        source_entropy=BehavioralRatioRuleConfig(
            baseline_floor=0.25,
            trigger_relative_deformation=0.3,
            full_score_relative_deformation=0.7,
        ),
        auth_failure=BehavioralRatioRuleConfig(
            baseline_floor=0.01,
            trigger_relative_deformation=1.0,
            full_score_relative_deformation=5.0,
        ),
        syn_ack=BehavioralRatioRuleConfig(
            baseline_floor=0.1,
            trigger_relative_deformation=0.5,
            full_score_relative_deformation=2.0,
            ratio_ceiling=20.0,
        ),
        rpc_amplification=BehavioralRatioRuleConfig(
            baseline_floor=0.05,
            trigger_relative_deformation=1.0,
            full_score_relative_deformation=5.0,
            ratio_ceiling=20.0,
        ),
        path_entropy=BehavioralRatioRuleConfig(
            baseline_floor=0.25,
            trigger_relative_deformation=0.3,
            full_score_relative_deformation=0.7,
        ),
        conversion=BehavioralRatioRuleConfig(
            baseline_floor=0.01,
            trigger_relative_deformation=0.3,
            full_score_relative_deformation=0.8,
        ),
        interarrival_variation=SequenceRatioRuleConfig(
            baseline_floor=0.1,
            trigger_relative_deformation=0.4,
            full_score_relative_deformation=0.8,
            minimum_points=5,
        ),
        crowd_coherence=SequenceRatioRuleConfig(
            baseline_floor=0.1,
            trigger_relative_deformation=0.4,
            full_score_relative_deformation=0.8,
            minimum_points=5,
        ),
    )


def log_template_config() -> LogTemplateConfig:
    return LogTemplateConfig(
        similarity_threshold=0.4,
        max_depth=4,
        max_children=100,
        max_clusters=10_000,
        parameterize_numeric_tokens=True,
        minimum_template_count=5,
        baseline_rate_floor=0.01,
        trigger_relative_deformation=2.0,
        full_score_relative_deformation=10.0,
    )
