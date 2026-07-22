"""Deployment configuration preserves bounded telemetry transport."""

from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_collector_raw_bus_batches_have_a_hard_item_ceiling() -> None:
    document = yaml.safe_load(
        (REPO_ROOT / "deploy" / "otel-collector" / "config.yaml").read_text(encoding="utf-8")
    )

    batch = document["processors"]["batch"]
    assert batch["send_batch_size"] == 256
    assert batch["send_batch_max_size"] == 256
    assert batch["send_batch_max_size"] <= batch["send_batch_size"]
