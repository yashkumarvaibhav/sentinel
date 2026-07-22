"""Small validated configuration factories shared by detector unit tests."""

from common.config import (
    BehavioralRatioConfig,
    BehavioralRatioRuleConfig,
    ChangePointSaturationConfig,
    DropRuleConfig,
    EdgeDegradationConfig,
    EdgeDegradationRuleConfig,
    EpisodeConfig,
    EpisodePolicyConfig,
    IngressRatioWindowConfig,
    LivenessConfig,
    LogTemplateConfig,
    SequenceRatioRuleConfig,
    SilenceRuleConfig,
)


def behavioral_ratio_config(
    *,
    ingress_service_mappings: dict[str, str] | None = None,
) -> BehavioralRatioConfig:
    return BehavioralRatioConfig(
        ingress_windows=IngressRatioWindowConfig(
            window_seconds=60,
            baseline_warmup_windows=1,
            minimum_window_requests=6,
            dedup_capacity=1_000,
            service_mappings=(
                {"frontend-proxy": "frontend"}
                if ingress_service_mappings is None
                else ingress_service_mappings
            ),
        ),
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
        window_seconds=60,
        baseline_warmup_windows=1,
        minimum_window_records=5,
        dedup_capacity=1_000,
        service_mappings={
            "frontend-proxy": "frontend",
            "checkout": "checkout",
            "cart": "cart",
            "payment": "payment",
        },
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


def change_point_saturation_config() -> ChangePointSaturationConfig:
    return ChangePointSaturationConfig(
        pelt_model="l2",
        pelt_penalty=0.01,
        minimum_series_points=12,
        minimum_segment_points=4,
        minimum_increasing_fraction=0.8,
        minimum_utilization_slope_per_second=0.0002,
        maximum_headroom_ratio=0.2,
        full_score_headroom_ratio=0.05,
    )


def liveness_config() -> LivenessConfig:
    return LivenessConfig(
        drop_rules={
            "frontend.request_rate": DropRuleConfig(
                minimum_expected_value=2.0,
                trigger_relative_drop=0.5,
                full_score_relative_drop=0.9,
            )
        },
        silence_rules={
            "frontend.request_rate": SilenceRuleConfig(
                maximum_age_seconds=120.0,
                full_score_age_seconds=300.0,
            )
        },
    )


def edge_degradation_config() -> EdgeDegradationConfig:
    return EdgeDegradationConfig(
        window_seconds=12,
        advance_seconds=2,
        baseline_warmup_samples=3,
        dedup_capacity=1000,
        rules=(
            EdgeDegradationRuleConfig(
                caller="frontend",
                downstream="checkout",
                rpc_service="oteldemo.CheckoutService",
                minimum_samples=5,
                latency_baseline_floor_ms=5.0,
                error_rate_baseline_floor=0.01,
                trigger_relative_latency_rise=0.5,
                full_score_relative_latency_rise=2.0,
                trigger_relative_error_rise=1.0,
                full_score_relative_error_rise=5.0,
            ),
            EdgeDegradationRuleConfig(
                caller="frontend",
                downstream="cart",
                rpc_service="oteldemo.CartService",
                minimum_samples=5,
                latency_baseline_floor_ms=5.0,
                error_rate_baseline_floor=0.01,
                trigger_relative_latency_rise=0.5,
                full_score_relative_latency_rise=2.0,
                trigger_relative_error_rise=1.0,
                full_score_relative_error_rise=5.0,
            ),
            EdgeDegradationRuleConfig(
                caller="checkout",
                downstream="payment",
                rpc_service="oteldemo.PaymentService",
                minimum_samples=5,
                latency_baseline_floor_ms=5.0,
                error_rate_baseline_floor=0.01,
                trigger_relative_latency_rise=0.5,
                full_score_relative_latency_rise=2.0,
                trigger_relative_error_rise=1.0,
                full_score_relative_error_rise=5.0,
            ),
        ),
    )


def episode_config(
    *,
    open_after_ticks: int = 3,
    close_after_ticks: int = 3,
    breach_score: float = 0.5,
    clear_score: float = 0.2,
) -> EpisodeConfig:
    policy = EpisodePolicyConfig(
        open_after_ticks=open_after_ticks,
        close_after_ticks=close_after_ticks,
        breach_score=breach_score,
        clear_score=clear_score,
    )
    return EpisodeConfig(policies={"RESIDUAL_EXCEED": policy, "SATURATION": policy})
