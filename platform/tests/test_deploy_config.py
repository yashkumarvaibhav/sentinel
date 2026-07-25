"""Deployment configuration preserves bounded telemetry transport."""

import re
from pathlib import Path
from typing import Any

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


# --- the owned edge proxy ---------------------------------------------------
#
# The frontend proxy is the only place a surgical remediation can land, so its
# configuration is vendored (see lab/testbed/envoy.tmpl.yaml). Vendoring buys a
# control surface and costs a coupling: these guard the coupling.


def _edge_template() -> dict[str, Any]:
    document = yaml.safe_load(
        (REPO_ROOT / "lab" / "testbed" / "envoy.tmpl.yaml").read_text(encoding="utf-8")
    )
    assert isinstance(document, dict)
    return document


def _edge_routes() -> list[dict[str, Any]]:
    listener = _edge_template()["static_resources"]["listeners"][0]
    manager = listener["filter_chains"][0]["filters"][0]["typed_config"]
    routes = manager["route_config"]["virtual_hosts"][0]["routes"]
    assert isinstance(routes, list)
    return routes


def test_the_vendored_edge_config_names_the_chart_it_was_taken_from() -> None:
    """A chart bump must not silently leave this file describing an older demo."""
    template = (REPO_ROOT / "lab" / "testbed" / "envoy.tmpl.yaml").read_text(encoding="utf-8")
    deploy = (REPO_ROOT / "lab" / "testbed" / "lab-deploy.sh").read_text(encoding="utf-8")

    vendored = re.search(r"^#\s+vendored-from-chart:\s*(\S+)$", template, re.MULTILINE)
    pinned = re.search(r"^CHART_VERSION=(\S+)$", deploy, re.MULTILINE)
    assert vendored is not None, "the vendored template must record the chart it came from"
    assert pinned is not None
    assert vendored.group(1) == pinned.group(1)


def test_every_cohort_is_addressable_at_the_edge() -> None:
    """A cohort the platform cannot restrain is a guard it cannot enforce."""
    cohorts = yaml.safe_load((REPO_ROOT / "config" / "cohorts.yml").read_text(encoding="utf-8"))
    limited = {
        route["typed_per_filter_config"]["envoy.filters.http.local_ratelimit"]["stat_prefix"]
        for route in _edge_routes()
        if "typed_per_filter_config" in route
    }

    for cohort in cohorts["cohorts"]:
        expected = "sentinel_" + cohort["cohort_id"].replace("-", "_")
        assert expected in limited, f"{cohort['cohort_id']} has no rate-limit route at the edge"


def test_refusing_and_slowing_a_cohort_are_two_mechanisms() -> None:
    """A rung is only a rung if it does something the other one does not.

    Refusing traffic over a ceiling (429) and delaying a share of it are
    genuinely different answers, so they are driven by different filters rather
    than by two dials on one - which would have been one rung wearing two names.
    """
    for route in _edge_routes():
        overrides = route.get("typed_per_filter_config")
        if overrides is None:
            continue
        limit = overrides["envoy.filters.http.local_ratelimit"]
        delay = overrides["envoy.filters.http.fault"]
        assert limit["token_bucket"]["max_tokens"] > 0, "a ceiling that refuses nothing"
        assert delay["delay"]["fixed_delay"], "a throttle that delays nothing"
        assert delay["delay_percent_runtime"].startswith("sentinel.throttle.")


def test_the_edge_ships_inert() -> None:
    """Every percentage is zero in both the filter defaults and the runtime layer.

    A rate limit that arrives switched on is an outage, and this is the property
    that lets the vendored config land on the testbed the scores are recorded
    against: with every key at zero the cohort routes are behaviourally
    identical to the catch-all they sit above.
    """
    for route in _edge_routes():
        overrides = route.get("typed_per_filter_config")
        if overrides is None:
            continue
        limit = overrides["envoy.filters.http.local_ratelimit"]
        for gate in ("filter_enabled", "filter_enforced"):
            assert limit[gate]["default_value"]["numerator"] == 0
            assert limit[gate]["runtime_key"].startswith("sentinel.ratelimit.")
        assert overrides["envoy.filters.http.fault"]["delay"]["percentage"]["numerator"] == 0

    layers = {layer["name"]: layer for layer in _edge_template()["layered_runtime"]["layers"]}
    published = layers["sentinel_static"]["static_layer"]["sentinel"]
    assert published["ratelimit"], "the cohort keys must be published so they are readable"
    for gates in published["ratelimit"].values():
        assert gates == {"enabled": 0, "enforced": 0}
    for gates in published["throttle"].values():
        assert gates == {"percent": 0}


def test_the_admin_layer_is_last_so_runtime_modify_wins() -> None:
    """Envoy refuses `/runtime_modify` without it, and an earlier layer would lose."""
    layers = [layer["name"] for layer in _edge_template()["layered_runtime"]["layers"]]
    assert layers[-1] == "admin_layer"
    assert "sentinel_static" in layers[:-1]


def test_the_listener_wide_rate_limit_can_restrain_nothing() -> None:
    """No token bucket at the listener: only routes that opt in are ever limited."""
    listener = _edge_template()["static_resources"]["listeners"][0]
    manager = listener["filter_chains"][0]["filters"][0]["typed_config"]
    filters = {entry["name"]: entry for entry in manager["http_filters"]}

    edge = filters["envoy.filters.http.local_ratelimit"]["typed_config"]
    assert "token_bucket" not in edge
    assert list(filters).index("envoy.filters.http.local_ratelimit") < list(filters).index(
        "envoy.filters.http.router"
    )


def test_the_cohort_routes_sit_above_the_catch_all_and_route_where_it_does() -> None:
    """Inert cohort routes must be indistinguishable from the route they precede."""
    routes = _edge_routes()
    catch_all = next(
        index for index, route in enumerate(routes) if route["match"] == {"prefix": "/"}
    )
    limited = [index for index, route in enumerate(routes) if "typed_per_filter_config" in route]

    assert limited, "no cohort route is wired at the edge"
    assert max(limited) < catch_all, "a cohort route below the catch-all can never match"
    for index in limited:
        assert routes[index]["route"]["cluster"] == routes[catch_all]["route"]["cluster"]


def test_the_edge_config_is_mounted_from_the_configmap_the_deploy_publishes() -> None:
    document = yaml.safe_load(
        (REPO_ROOT / "lab" / "testbed" / "otel-demo-values.yaml").read_text(encoding="utf-8")
    )
    deploy = (REPO_ROOT / "lab" / "testbed" / "lab-deploy.sh").read_text(encoding="utf-8")

    mounted = document["components"]["frontend-proxy"]["mountedConfigMaps"][0]
    assert mounted["mountPath"] == "/home/envoy/envoy.tmpl.yaml"
    assert mounted["subPath"] == "envoy.tmpl.yaml"
    assert f"ENVOY_CONFIGMAP={mounted['existingConfigMap']}" in deploy
    # The template is expanded once, at container start, so a changed ConfigMap
    # only reaches Envoy if something rolls the pod.
    assert "podAnnotations.sentinel-edge-config=${envoy_checksum}" in deploy
