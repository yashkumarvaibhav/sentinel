"""Deterministic JSON Schema export for the public cross-plane contracts."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import cast

from pydantic import BaseModel
from pydantic.json_schema import JsonSchemaValue, models_json_schema

from contracts.context import ContextWindow
from contracts.decision import (
    AgentAssessment,
    ChangeEvent,
    Incident,
    Verdict,
    Verification,
)
from contracts.detection import DecompFrame, Symptom, SymptomEpisode
from contracts.telemetry import Observation

JSON_SCHEMA_DIALECT = "https://json-schema.org/draft/2020-12/schema"
SCHEMA_ID = "urn:sentinel:contracts:v1"
CONTRACT_SCHEMA_PATH = Path(__file__).with_name("schema.json")
PUBLIC_MODELS: tuple[type[BaseModel], ...] = (
    Observation,
    ContextWindow,
    DecompFrame,
    Symptom,
    SymptomEpisode,
    ChangeEvent,
    AgentAssessment,
    Verdict,
    Incident,
    Verification,
)


def build_contract_schema() -> JsonSchemaValue:
    """Build one schema whose named definitions cover every public contract."""
    _, schema = models_json_schema(
        [(model, "validation") for model in PUBLIC_MODELS],
        by_alias=True,
        title="SentinelContract",
        description="Public contracts exchanged between Sentinel planes.",
    )
    schema["$schema"] = JSON_SCHEMA_DIALECT
    schema["$id"] = SCHEMA_ID
    schema["oneOf"] = [{"$ref": f"#/$defs/{model.__name__}"} for model in PUBLIC_MODELS]
    return schema


def render_contract_schema() -> str:
    """Render canonical JSON so repeated exports are byte-identical."""
    return json.dumps(build_contract_schema(), indent=2, sort_keys=True) + "\n"


def export_contract_schema(output: Path, *, check: bool = False) -> int:
    """Write the schema, or return non-zero when a checked artifact has drifted."""
    rendered = render_contract_schema()
    if check:
        existing = output.read_text(encoding="utf-8") if output.is_file() else None
        if existing != rendered:
            print(
                f"contract schema is stale: run python -m contracts export --output {output}",
                file=sys.stderr,
            )
            return 1
        return 0

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(rendered, encoding="utf-8")
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m contracts")
    commands = parser.add_subparsers(dest="command", required=True)
    export = commands.add_parser("export", help="export the combined contract JSON Schema")
    export.add_argument("--output", type=Path, default=CONTRACT_SCHEMA_PATH)
    export.add_argument(
        "--check",
        action="store_true",
        help="fail if the output differs instead of rewriting it",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the contract artifact command line."""
    args = _parser().parse_args(argv)
    if args.command == "export":
        output = cast(Path, args.output)
        check = cast(bool, args.check)
        return export_contract_schema(output, check=check)
    raise AssertionError(f"unhandled command: {args.command}")
