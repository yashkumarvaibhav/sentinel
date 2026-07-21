"""Scoring metrics refuse to manufacture certainty from missing samples."""

from __future__ import annotations

from pathlib import Path

import pytest
from lab.scoring.gates import load_gate_config
from lab.scoring.metrics import binary_metrics, nearest_rank


def test_binary_residual_metrics_count_confusion_matrix_exactly() -> None:
    metrics = binary_metrics(
        predicted=(True, True, False, False),
        expected=(True, False, True, False),
    )

    assert (metrics.true_positive, metrics.false_positive) == (1, 1)
    assert (metrics.false_negative, metrics.true_negative) == (1, 1)
    assert metrics.precision.status == "ok"
    assert metrics.precision.value == 0.5
    assert metrics.recall.status == "ok"
    assert metrics.recall.value == 0.5
    assert metrics.false_positive_rate.status == "ok"
    assert metrics.false_positive_rate.value == 0.5


def test_empty_denominators_are_insufficient_instead_of_fake_zeroes() -> None:
    metrics = binary_metrics(predicted=(False, False), expected=(False, False))

    assert metrics.precision.status == "insufficient"
    assert metrics.precision.value is None
    assert metrics.recall.status == "insufficient"
    assert metrics.recall.value is None
    assert metrics.false_positive_rate.status == "ok"
    assert metrics.false_positive_rate.value == 0.0


def test_nearest_rank_percentiles_are_documented_and_explicit_on_empty_input() -> None:
    assert nearest_rank((4.0, 1.0, 3.0, 2.0), percentile=0.5).value == 2.0
    assert nearest_rank((4.0, 1.0, 3.0, 2.0), percentile=0.95).value == 4.0
    assert nearest_rank((), percentile=0.5).status == "insufficient"


def test_duplicate_score_gate_keys_are_rejected(tmp_path: Path) -> None:
    path = tmp_path / "config.yml"
    path.write_text("version: 1\nversion: 1\n", encoding="utf-8")

    with pytest.raises(ValueError, match="duplicate key"):
        load_gate_config(path)
