"""Small, bounded ClickHouse HTTP helpers shared by migrations and repositories."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import cast

import httpx


async def execute(
    client: httpx.AsyncClient,
    query: str,
    *,
    parameters: Mapping[str, str | int] | None = None,
    body: str | None = None,
) -> None:
    """Execute one statement, with values carried as ClickHouse HTTP parameters."""
    params = _parameters(parameters)
    if body is None:
        response = await client.post("/", params=params, content=query)
    else:
        params["query"] = query
        response = await client.post(
            "/",
            params=params,
            content=body,
            headers={"content-type": "application/x-ndjson"},
        )
    _raise_for_error(response)


async def rows(
    client: httpx.AsyncClient,
    query: str,
    *,
    parameters: Mapping[str, str | int] | None = None,
) -> tuple[dict[str, object], ...]:
    """Return a JSONEachRow query as checked object rows."""
    response = await client.post("/", params=_parameters(parameters), content=query)
    _raise_for_error(response)
    decoded_rows: list[dict[str, object]] = []
    for line in response.text.splitlines():
        decoded = json.loads(line)
        if not isinstance(decoded, dict):
            raise RuntimeError("ClickHouse returned a non-object JSONEachRow result")
        decoded_rows.append(cast(dict[str, object], decoded))
    return tuple(decoded_rows)


def encode_json_each_row(records: Sequence[Mapping[str, object]]) -> str:
    """Encode insert rows deterministically for retries and reproducible tests."""
    return "".join(
        json.dumps(record, allow_nan=False, separators=(",", ":"), sort_keys=True) + "\n"
        for record in records
    )


def _parameters(parameters: Mapping[str, str | int] | None) -> dict[str, str | int]:
    return {f"param_{name}": value for name, value in (parameters or {}).items()}


def _raise_for_error(response: httpx.Response) -> None:
    if response.is_error:
        detail = response.text.strip()[:500]
        raise RuntimeError(f"ClickHouse query failed ({response.status_code}): {detail}")
