"""The API gateway.

One HTTP surface in front of the planes. For now it answers the two questions
that have to be answerable from the first day: what is running, and is it
healthy.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

import httpx
from fastapi import FastAPI, Response

from api.audit import AuditLedger
from api.decomposition import (
    MAX_FRAMES,
    DecompositionRangeError,
    DecompositionReader,
    resolve_window,
    serialize_window,
)
from api.gate import SharedSecretGate
from api.health import HealthReport, Probe, Readiness, check_health
from api.probes import platform_probes
from audit import verify_chain
from common.buildinfo import build_info
from common.config import load_config
from common.settings import Settings, settings
from common.storage import ClickHouseRepository

SERVICE = "sentinel-gateway"

# Routes that expose more than the command centre needs and so ride the gate
# even for reads. The audit ledger is here because it is the record of what the
# platform did and why - the one read where "who is asking" matters as much as
# for a write (build decision #12 names it).
_SENSITIVE_PREFIXES = ("/api/lab", "/api/audit")

# How much of the chain one request may ask for. A ledger read is a scan in
# sequence order, so an unbounded one is a way to make the gateway do arbitrary
# work on behalf of anybody who can reach it.
MAX_AUDIT_ENTRIES = 500


def create_app(
    config: Settings | None = None,
    probes: Mapping[str, Probe] | None = None,
    ledger: AuditLedger | None = None,
    decomposition_reader: DecompositionReader | None = None,
) -> FastAPI:
    """Build the gateway application.

    ``probes`` is injectable so tests exercise the endpoints without a stack.
    """
    config = config or settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.runtime_config = load_config(config.config_dir)
        # Injectable so the route is testable without a database. Left unset the
        # endpoint answers 503 rather than pretending the ledger is empty: "no
        # ledger attached" and "nothing has happened" must not look the same.
        app.state.audit_ledger = ledger
        # Same rule as the ledger: unset answers 503 rather than pretending the
        # store is empty. "No store attached" and "nothing happened" must not
        # look the same.
        async with httpx.AsyncClient(timeout=config.probe_timeout_seconds) as client:
            app.state.probes = probes if probes is not None else platform_probes(config, client)
            if decomposition_reader is not None:
                app.state.decomposition_reader = decomposition_reader
                yield
                return
            # Its own client: the probe client carries a short timeout tuned for
            # liveness checks, and a full-resolution window is a read, not a ping.
            auth = (config.clickhouse_user, config.clickhouse_password.get_secret_value())
            async with httpx.AsyncClient(
                base_url=config.clickhouse_url,
                auth=auth,
                timeout=config.storage_timeout_seconds,
            ) as store:
                app.state.decomposition_reader = ClickHouseRepository(
                    client=store, database=config.clickhouse_database
                )
                yield

    app = FastAPI(
        title="Sentinel API",
        summary="Context-aware autonomous observability",
        version=build_info().version,
        lifespan=lifespan,
    )

    # Interim gate: inert until a secret is configured (see api/gate.py).
    app.add_middleware(
        SharedSecretGate,
        secret=config.shared_secret,
        sensitive_prefixes=_SENSITIVE_PREFIXES,
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

    @app.get("/api/audit", tags=["audit"])
    async def audit(
        response: Response, limit: int = 100, after: int | None = None
    ) -> dict[str, Any]:
        """The ledger, in order, with the chain re-verified on the way out.

        The verification is not decoration. These rows came out of a database
        somebody could have edited directly, which is precisely the attack the
        chain exists to detect, so the answer says whether what it is returning
        can be trusted rather than leaving that to the reader.
        """
        ledger = getattr(app.state, "audit_ledger", None)
        if ledger is None:
            response.status_code = 503
            return {
                "detail": "no audit ledger is attached to this gateway",
                "entries": [],
                "intact": False,
            }
        bounded = max(1, min(limit, MAX_AUDIT_ENTRIES))
        entries = await ledger.entries(limit=bounded, after_sequence=after)
        verification = verify_chain(entries)
        if not verification.intact:
            # A broken chain is not a 200 with a footnote. Something is wrong
            # with the record of what this platform did.
            response.status_code = 409
        return {
            "entries": [entry.model_dump(mode="json") for entry in entries],
            "count": len(entries),
            "intact": verification.intact,
            "head_hash": verification.head_hash,
            "broken_at": verification.broken_at,
            "detail": verification.detail,
        }

    @app.get("/api/decomposition", tags=["decomposition"])
    async def decomposition(
        response: Response,
        service: str,
        signal: str,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int = 1_000,
    ) -> dict[str, Any]:
        """The surge split into what is explained and what is not, over a window.

        An empty window answers 200 with zero frames. "Nothing was recorded for
        this service here" is a true and useful answer; a 404 would claim the
        route does not exist and send a reader hunting for a typo.
        """
        reader = getattr(app.state, "decomposition_reader", None)
        if reader is None:
            response.status_code = 503
            return {
                "detail": "no decomposition store is attached to this gateway",
                "frames": [],
                "count": 0,
            }
        try:
            window_start, window_end = resolve_window(start, end, now=datetime.now(UTC))
        except DecompositionRangeError as refusal:
            response.status_code = 400
            return {"detail": str(refusal), "frames": [], "count": 0}
        bounded = max(1, min(limit, MAX_FRAMES))
        frames = await reader.list_decomp_frames(
            service=service,
            signal=signal,
            start=window_start,
            end=window_end,
            limit=bounded,
        )
        return serialize_window(
            frames,
            service=service,
            signal=signal,
            start=window_start,
            end=window_end,
            limit=bounded,
        )

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
