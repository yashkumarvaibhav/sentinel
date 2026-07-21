"""The real sports connector is bounded, cached and optional."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import Any, cast

import httpx

from common.config import SportsConnectorConfig
from context.football_data import FootballDataConnector

_AS_OF = datetime(2026, 7, 21, 19, 30, tzinfo=UTC)


def test_fixture_maps_to_active_context_and_success_is_cached() -> None:
    asyncio.run(_mapping_case())


async def _mapping_case() -> None:
    requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        assert request.headers["X-Auth-Token"] == "test-secret"
        assert request.url.path == "/v4/competitions/CL/matches"
        assert request.url.params["dateFrom"] == "2026-07-20"
        assert request.url.params["dateTo"] == "2026-07-23"
        return httpx.Response(200, json=_response())

    async with httpx.AsyncClient(
        base_url="https://api.football-data.org",
        transport=httpx.MockTransport(handler),
    ) as client:
        connector = FootballDataConnector(
            client=client,
            api_key="test-secret",
            configuration=_configuration(),
        )

        first = await connector.windows(as_of=_AS_OF)
        second = await connector.windows(as_of=_AS_OF)

    assert first == second
    assert len(first) == 1
    window = first[0]
    assert window.context_id == "football-data-match-98765"
    assert window.name == "North City vs South United (Champions League)"
    assert window.source == "football-data.org"
    assert window.honesty == "REAL"
    assert window.expected_delta == {"frontend.request_rate": 2.25}
    assert (window.valid_from.hour, window.valid_from.minute) == (18, 30)
    assert (window.valid_to.hour, window.valid_to.minute) == (21, 30)
    assert requests == 1
    assert (connector.stats().requests, connector.stats().cache_hits) == (1, 1)


def test_zero_key_and_upstream_failure_return_empty_without_retries() -> None:
    asyncio.run(_fallback_case())


async def _fallback_case() -> None:
    requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(503, text="unavailable")

    async with httpx.AsyncClient(
        base_url="https://api.football-data.org",
        transport=httpx.MockTransport(handler),
    ) as client:
        disabled = FootballDataConnector(
            client=client,
            api_key="",
            configuration=_configuration(),
        )
        failing = FootballDataConnector(
            client=client,
            api_key="test-secret",
            configuration=_configuration(),
        )

        assert await disabled.windows(as_of=_AS_OF) == ()
        assert await failing.windows(as_of=_AS_OF) == ()
        assert await failing.windows(as_of=_AS_OF) == ()

    assert requests == 1
    assert disabled.stats().disabled == 1
    assert (failing.stats().failures, failing.stats().cache_hits) == (1, 1)


def test_cancelled_or_out_of_window_fixtures_do_not_claim_context() -> None:
    asyncio.run(_filter_case())


async def _filter_case() -> None:
    payload = _response()
    payload["matches"][0]["status"] = "CANCELLED"

    async with httpx.AsyncClient(
        base_url="https://api.football-data.org",
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload)),
    ) as client:
        connector = FootballDataConnector(
            client=client,
            api_key="test-secret",
            configuration=_configuration(),
        )
        assert await connector.windows(as_of=_AS_OF) == ()


def _configuration() -> SportsConnectorConfig:
    return SportsConnectorConfig(
        enabled=True,
        provider="football-data.org",
        competition_codes=("CL",),
        lead_minutes=30,
        duration_minutes=150,
        expected_delta={"frontend.request_rate": 2.25},
        trust_score=0.9,
    )


def _response() -> dict[str, Any]:
    return cast(
        dict[str, Any],
        json.loads(
            """
        {
          "matches": [
            {
              "id": 98765,
              "utcDate": "2026-07-21T19:00:00Z",
              "status": "TIMED",
              "competition": {"name": "Champions League", "code": "CL"},
              "homeTeam": {"name": "North City"},
              "awayTeam": {"name": "South United"}
            }
          ]
        }
        """
        ),
    )
