"""Concrete readiness probes for the platform's dependencies.

Each probe is deliberately shallow — it answers "is this reachable right now",
not "is it healthy in every respect". Deep checks belong in meta-monitoring,
where they can be sampled on a schedule instead of on every health request.
"""

from __future__ import annotations

import asyncio

import httpx

from api.health import Probe
from common.settings import Settings


def http_probe(client: httpx.AsyncClient, url: str) -> Probe:
    """Probe that succeeds when a URL answers with a non-error status."""

    async def probe() -> None:
        response = await client.get(url)
        response.raise_for_status()

    return probe


def tcp_probe(host: str, port: int) -> Probe:
    """Probe that succeeds when a TCP connection can be opened and closed."""

    async def probe() -> None:
        _, writer = await asyncio.open_connection(host, port)
        writer.close()
        await writer.wait_closed()

    return probe


def platform_probes(settings: Settings, client: httpx.AsyncClient) -> dict[str, Probe]:
    """The dependency set the gateway reports on."""
    return {
        "postgres": tcp_probe(settings.postgres_host, settings.postgres_port),
        "redpanda": tcp_probe(*settings.first_broker),
        "clickhouse": http_probe(client, f"{settings.clickhouse_url}/ping"),
        "victoriametrics": http_probe(client, f"{settings.victoriametrics_url}/health"),
        "loki": http_probe(client, f"{settings.loki_url}/ready"),
        "tempo": http_probe(client, f"{settings.tempo_url}/ready"),
        "otel-collector": http_probe(client, f"{settings.collector_url}/"),
    }
