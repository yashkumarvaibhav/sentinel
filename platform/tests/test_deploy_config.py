"""Deployment configuration preserves bounded telemetry transport."""

from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_collector_raw_bus_batches_have_a_hard_item_ceiling() -> None:
    document = yaml.safe_load(
        (REPO_ROOT / "deploy" / "otel-collector" / "config.yaml").read_text(encoding="utf-8")
    )

    batch = document["processors"]["batch"]
    assert batch["send_batch_size"] == 128
    assert batch["send_batch_max_size"] == 128
    assert batch["send_batch_max_size"] <= batch["send_batch_size"]


def test_k3d_collector_can_reach_local_kubelet_at_detector_cadence() -> None:
    document = yaml.safe_load(
        (REPO_ROOT / "lab" / "testbed" / "otel-demo-values.yaml").read_text(encoding="utf-8")
    )

    collector = document["opentelemetry-collector"]
    kubelet = collector["config"]["receivers"]["kubeletstats"]
    assert collector["hostNetwork"] is True
    assert collector["dnsPolicy"] == "ClusterFirstWithHostNet"
    assert kubelet["endpoint"] == "127.0.0.1:10250"
    assert kubelet["collection_interval"] == "10s"


def test_each_k3d_node_exports_its_own_container_limit_evidence() -> None:
    document = yaml.safe_load(
        (REPO_ROOT / "lab" / "testbed" / "otel-demo-values.yaml").read_text(encoding="utf-8")
    )

    collector = document["opentelemetry-collector"]
    cluster = collector["config"]["receivers"]["k8s_cluster"]
    assert collector["mode"] == "daemonset"
    assert cluster["collection_interval"] == "10s"
    assert cluster.get("k8s_leader_elector") is None


def test_checkout_has_a_hard_limit_above_its_measured_oom_footprint() -> None:
    document = yaml.safe_load(
        (REPO_ROOT / "lab" / "testbed" / "otel-demo-values.yaml").read_text(encoding="utf-8")
    )

    assert document["components"]["checkout"]["resources"]["limits"]["memory"] == "64Mi"


def test_demo_load_generator_is_disabled_in_favor_of_capped_scenario_jobs() -> None:
    document = yaml.safe_load(
        (REPO_ROOT / "lab" / "testbed" / "otel-demo-values.yaml").read_text(encoding="utf-8")
    )

    assert document["components"]["load-generator"]["enabled"] is False


def test_checkout_journey_preallocates_for_slow_successful_iterations() -> None:
    script = (REPO_ROOT / "lab" / "loadgen" / "scenario.js").read_text(encoding="utf-8")

    assert "const preAllocatedVUs = journey === 'checkout'" in script
    assert "Math.min(Math.max(phase.rate_rps * 20, 10), 50)" in script
    assert "preAllocatedVUs," in script
    assert "dropped_iterations: ['count==0']" in script


def test_frontend_emitter_failure_is_single_target_bounded_and_self_expiring() -> None:
    document = yaml.safe_load(
        (REPO_ROOT / "lab" / "testbed" / "chaos" / "frontend-proxy-pod-failure.yaml").read_text(
            encoding="utf-8"
        )
    )

    assert document["kind"] == "PodChaos"
    assert document["metadata"] == {
        "name": "frontend-proxy-pod-failure",
        "namespace": "otel-demo",
    }
    assert document["spec"]["action"] == "pod-failure"
    assert document["spec"]["mode"] == "one"
    assert document["spec"]["duration"] == "220s"
    assert document["spec"]["selector"] == {
        "namespaces": ["otel-demo"],
        "labelSelectors": {"app.kubernetes.io/component": "frontend-proxy"},
    }
