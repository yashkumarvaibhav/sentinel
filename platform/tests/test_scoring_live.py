"""Live scoring keeps workload targets and extraction queries contained."""

from __future__ import annotations

from pathlib import Path

from lab.scenarios import SeedPurpose, compile_profile, load_profile
from lab.scoring.live import build_k6_job, expected_request_count

SCENARIO_ROOT = Path(__file__).resolve().parents[2] / "lab" / "scenarios"


def test_live_job_is_resource_capped_in_namespace_with_no_target_override() -> None:
    profile = load_profile(SCENARIO_ROOT / "match_night.yml")
    artifacts = compile_profile(
        profile,
        seed=profile.seeds.development[0],
        purpose=SeedPurpose.DEVELOPMENT,
    )

    job = build_k6_job(artifacts.schedule, job_name="score-match-211", run_id="run-abc")
    container = job["spec"]["template"]["spec"]["containers"][0]

    assert job["metadata"]["namespace"] == "otel-demo"
    assert container["image"] == "grafana/k6:0.55.0"
    assert container["resources"]["limits"] == {"cpu": "1", "memory": "256Mi"}
    env = {item["name"]: item["value"] for item in container["env"]}
    assert "TARGET" not in env
    assert env["SENTINEL_RUN_ID"] == "run-abc"
    assert expected_request_count(artifacts.schedule) == 712
