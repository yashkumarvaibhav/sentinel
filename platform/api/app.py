"""The API gateway.

One HTTP surface in front of the planes. For now it answers the two questions
that have to be answerable from the first day: what is running, and is it
healthy.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import FastAPI, Response

from api.health import HealthReport, Probe, Readiness, check_health
from api.probes import platform_probes
from common.buildinfo import build_info
from common.settings import Settings, settings

SERVICE = "sentinel-gateway"


def create_app(
    config: Settings | None = None,
    probes: Mapping[str, Probe] | None = None,
) -> FastAPI:
    """Build the gateway application.

    ``probes`` is injectable so tests exercise the endpoints without a stack.
    """
    config = config or settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with httpx.AsyncClient(timeout=config.probe_timeout_seconds) as client:
            app.state.probes = probes if probes is not None else platform_probes(config, client)
            yield

    app = FastAPI(
        title="Sentinel API",
        summary="Context-aware autonomous observability",
        version=build_info().version,
        lifespan=lifespan,
    )

    @app.get("/api/version", tags=["meta"])
    async def version() -> dict[str, str]:
        """Which code is actually running here."""
        info = build_info()
        return {
            "service": SERVICE,
            "version": info.version,
            "git_sha": info.git_sha,
            "short_sha": info.short_sha,
            "built_at": info.built_at,
            "env": config.env,
        }

    @app.get("/api/health", tags=["meta"])
    async def health(response: Response) -> dict[str, Any]:
        """Per-dependency readiness, plus the list of what is degraded."""
        report = await check_health(app.state.probes, config.probe_timeout_seconds)
        if report.status is Readiness.DEGRADED:
            # Degraded is a real answer, not a server error: the gateway is up
            # and telling the truth about what beneath it is not.
            response.status_code = 503
        return _serialize(report)

    return app


def _serialize(report: HealthReport) -> dict[str, Any]:
    return {
        "status": report.status.value,
        "degraded": list(report.degraded),
        "components": [
            {
                "name": component.name,
                "ready": component.ready,
                "latency_ms": component.latency_ms,
                "detail": component.detail,
            }
            for component in report.components
        ],
    }


app = create_app()
