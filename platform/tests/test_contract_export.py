"""The checked contract schema is deterministic and names every public model."""

from __future__ import annotations

import json
from pathlib import Path

from contracts.schema import (
    CONTRACT_SCHEMA_PATH,
    PUBLIC_MODELS,
    build_contract_schema,
    main,
    render_contract_schema,
)


def test_combined_schema_exposes_every_public_contract() -> None:
    schema = build_contract_schema()

    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["$id"] == "urn:sentinel:contracts:v1"
    assert [entry["$ref"] for entry in schema["oneOf"]] == [
        f"#/$defs/{model.__name__}" for model in PUBLIC_MODELS
    ]
    assert {model.__name__ for model in PUBLIC_MODELS}.issubset(schema["$defs"])


def test_rendered_schema_is_canonical_json() -> None:
    first = render_contract_schema()
    second = render_contract_schema()

    assert first == second
    assert first.endswith("\n")
    assert json.dumps(json.loads(first), indent=2, sort_keys=True) + "\n" == first


def test_checked_in_schema_matches_python_models() -> None:
    assert CONTRACT_SCHEMA_PATH.read_text(encoding="utf-8") == render_contract_schema()


def test_export_command_writes_requested_path(tmp_path: Path) -> None:
    output = tmp_path / "nested" / "contracts.schema.json"

    assert main(["export", "--output", str(output)]) == 0
    assert output.read_text(encoding="utf-8") == render_contract_schema()


def test_export_check_reports_drift_without_rewriting(tmp_path: Path) -> None:
    output = tmp_path / "contracts.schema.json"
    output.write_text("{}\n", encoding="utf-8")

    assert main(["export", "--output", str(output), "--check"]) == 1
    assert output.read_text(encoding="utf-8") == "{}\n"
