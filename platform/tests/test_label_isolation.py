"""Ground truth stays outside every runtime decision package."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from pydantic import ValidationError

from contracts import Observation

PLATFORM_ROOT = Path(__file__).resolve().parents[1]
RUNTIME_PACKAGES = ("detection", "decision", "rca", "action")


def test_runtime_import_graph_never_reaches_lab_or_label_modules() -> None:
    violations: list[str] = []
    for package in RUNTIME_PACKAGES:
        for path in sorted((PLATFORM_ROOT / package).rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Constant)
                    and isinstance(node.value, str)
                    and ("labels.json" in node.value or "lab/scenarios" in node.value)
                ):
                    violations.append(
                        f"{path.relative_to(PLATFORM_ROOT)} -> literal {node.value!r}"
                    )
                modules: tuple[str, ...]
                if isinstance(node, ast.Import):
                    modules = tuple(alias.name for alias in node.names)
                elif isinstance(node, ast.ImportFrom):
                    modules = () if node.module is None else (node.module,)
                else:
                    continue
                for module in modules:
                    if module == "lab" or module.startswith("lab.") or "label" in module.split("."):
                        violations.append(f"{path.relative_to(PLATFORM_ROOT)} -> {module}")

    assert violations == []


def test_runtime_observation_guard_rejects_private_answer_keys() -> None:
    with pytest.raises(ValidationError, match="ground-truth"):
        Observation.model_validate(
            {
                "observation_id": "guarded",
                "ts": "2026-07-21T12:00:00Z",
                "service": "frontend",
                "signal": "request_rate",
                "value": 1.0,
                "attributes": {"injected_fault_id": "private-answer"},
            }
        )
