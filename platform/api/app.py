"""The API gateway.

One HTTP surface in front of the planes. For now it answers the two questions
that have to be answerable from the first day: what is running, and is it
healthy.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any, Protocol

import httpx
from fastapi import FastAPI, Response
from fastapi.responses import StreamingResponse

from action.control import ActionControlNotFoundError, ActionControlTransitionError
from action.runtime import ActionControlRuntime, build_action_runtime
from api.action_control import (
    ActionControlStore,
    action_control_response,
    unavailable_action_control,
)
from api.audit import AuditLedger
from api.causal_graph import (
    CausalGraphDataError,
    CausalGraphReader,
    causal_graph_snapshot,
    unavailable_causal_graph,
)
from api.decomposition import (
    MAX_FRAMES,
    DecompositionRangeError,
    DecompositionReader,
    resolve_window,
    serialize_window,
)
from api.gate import SharedSecretGate
from api.health import HealthReport, Probe, Readiness, check_health
from api.incident_detail import (
    IncidentDetailDataError,
    incident_detail_snapshot,
    unavailable_incident_detail,
)
from api.incidents import (
    DEFAULT_INCIDENTS,
    MAX_INCIDENTS,
    IncidentFeedDataError,
    IncidentFeedPublisher,
    IncidentFeedReader,
    incident_snapshot,
    unavailable_incident_snapshot,
)
from api.kpis import (
    ScoreProofUnavailableError,
    build_kpi_response,
    load_score_proof,
    unavailable_kpi_response,
)
from api.probes import platform_probes
from api.security import (
    SecuritySnapshotDataError,
    SecuritySnapshotReader,
    security_snapshot,
    unavailable_security_snapshot,
)
from api.stream import StreamBroker, snapshot_invalidation, stream_response
from audit import verify_chain
from common.buildinfo import build_info
from common.config import load_config
from common.settings import Settings, settings
from common.storage import (
    ClickHouseRepository,
    IncidentDetailRecord,
    PostgresRepository,
    create_postgres_pool,
)
from contracts import (
    ActionControlRequest,
    ActionControlResponse,
    CausalGraphResponse,
    IncidentDetailResponse,
    IncidentFeedResponse,
    KpiResponse,
    ScoreProof,
    SecurityResponse,
    SnapshotResource,
)

SERVICE = "sentinel-gateway"
LOGGER = logging.getLogger(__name__)

# Routes that expose more than the public command shell needs and so ride the
# gate even for reads. The audit ledger records what the platform did and why;
# the per-incident proof joins the full evidence and action history. Both need
# the interim identity check named by the architecture until OIDC replaces it.
_SENSITIVE_PREFIXES = (
    "/api/lab",
    "/api/audit",
    "/api/incidents/",
    "/api/security",
)

# How much of the chain one request may ask for. A ledger read is a scan in
# sequence order, so an unbounded one is a way to make the gateway do arbitrary
# work on behalf of anybody who can reach it.
MAX_AUDIT_ENTRIES = 500


class IncidentDetailReader(Protocol):
    """Structural marker documented by the injected get-by-id method."""

    async def get_incident_detail(self, incident_id: str) -> IncidentDetailRecord | None: ...


def create_app(
    config: Settings | None = None,
    probes: Mapping[str, Probe] | None = None,
    ledger: AuditLedger | None = None,
    decomposition_reader: DecompositionReader | None = None,
    stream_broker: StreamBroker | None = None,
    score_proof: ScoreProof | None = None,
    incident_reader: IncidentFeedReader | None = None,
    causal_graph_reader: CausalGraphReader | None = None,
    incident_detail_reader: IncidentDetailReader | None = None,
    action_control_store: ActionControlStore | None = None,
    security_reader: SecuritySnapshotReader | None = None,
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
        app.state.stream_broker = stream_broker if stream_broker is not None else StreamBroker()
        app.state.incident_reader = incident_reader
        app.state.causal_graph_reader = causal_graph_reader
        app.state.incident_detail_reader = incident_detail_reader
        app.state.action_control_store = action_control_store
        app.state.security_reader = security_reader
        app.state.incident_publisher = None
        incident_pool = None
        action_runtime: ActionControlRuntime | None = None
        action_stop: asyncio.Event | None = None
        action_task: asyncio.Task[None] | None = None
        if incident_reader is None and probes is None:
            incident_pool = create_postgres_pool(config)
            await incident_pool.open(wait=True)
            incident_store = PostgresRepository(
                pool=incident_pool,
                schema=config.postgres_schema,
            )
            app.state.incident_reader = incident_store
            app.state.causal_graph_reader = incident_store
            app.state.incident_detail_reader = incident_store
            app.state.security_reader = incident_store
            if action_control_store is None:
                app.state.action_control_store = incident_store
            app.state.incident_publisher = IncidentFeedPublisher(
                store=incident_store,
                broker=app.state.stream_broker,
            )
            if config.action_worker_enabled:
                action_runtime = build_action_runtime(
                    store=incident_store,
                    config_dir=config.config_dir,
                    victoriametrics_url=config.victoriametrics_url,
                    victoriametrics_timeout_seconds=config.storage_timeout_seconds,
                    worker_id=config.action_worker_id,
                    poll_interval_seconds=config.action_poll_interval_seconds,
                    batch_limit=config.action_poll_batch_limit,
                    settlement_delay_seconds=config.action_settlement_delay_seconds,
                    slo_window_seconds=config.action_slo_window_seconds,
                    after_commit=lambda _control: app.state.stream_broker.publish(
                        snapshot_invalidation(
                            SnapshotResource.INCIDENTS,
                            SnapshotResource.ACTIONS,
                            SnapshotResource.SECURITY,
                        )
                    ),
                )
                action_stop = asyncio.Event()
                action_task = asyncio.create_task(
                    action_runtime.run(action_stop),
                    name="sentinel-action-control-runtime",
                )
        try:
            app.state.score_proof = (
                score_proof
                if score_proof is not None
                else load_score_proof(config.score_proof_path)
            )
            app.state.score_proof_error = None
        except ScoreProofUnavailableError as exc:
            app.state.score_proof = None
            app.state.score_proof_error = str(exc)
        # Same rule as the ledger: unset answers 503 rather than pretending the
        # store is empty. "No store attached" and "nothing happened" must not
        # look the same.
        try:
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
        finally:
            if action_stop is not None:
                action_stop.set()
            if action_task is not None:
                try:
                    await asyncio.wait_for(action_task, timeout=5.0)
                except TimeoutError:
                    action_task.cancel()
                    await asyncio.gather(action_task, return_exceptions=True)
            if action_runtime is not None:
                action_runtime.close()
            if incident_pool is not None:
                await incident_pool.close()

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

    @app.get("/stream/events", tags=["stream"], response_class=StreamingResponse)
    async def stream() -> StreamingResponse:
        """Typed live invalidations; authoritative state remains on REST."""
        return stream_response(app.state.stream_broker)

    @app.get("/api/kpis", tags=["reliability"], response_model=KpiResponse)
    async def kpis(response: Response) -> KpiResponse:
        """Four reliability questions, preserving every insufficient state."""
        proof: ScoreProof | None = app.state.score_proof
        if proof is None:
            response.status_code = 503
            return unavailable_kpi_response(
                app.state.score_proof_error or "score proof unavailable"
            )
        return build_kpi_response(proof)

    @app.get("/api/incidents", tags=["incidents"], response_model=IncidentFeedResponse)
    async def incidents(
        response: Response,
        limit: int = DEFAULT_INCIDENTS,
    ) -> IncidentFeedResponse:
        """The bounded authoritative live-incident snapshot, latest first."""
        bounded = max(1, min(limit, MAX_INCIDENTS))
        reader: IncidentFeedReader | None = getattr(app.state, "incident_reader", None)
        if reader is None:
            response.status_code = 503
            return unavailable_incident_snapshot(
                limit=bounded,
                detail="no live incident store is attached to this gateway",
            )
        try:
            records = await reader.list_incidents(limit=bounded)
            return incident_snapshot(records, limit=bounded)
        except IncidentFeedDataError as exc:
            response.status_code = 503
            return unavailable_incident_snapshot(limit=bounded, detail=str(exc))
        except Exception:
            LOGGER.exception("live incident snapshot read failed")
            response.status_code = 503
            return unavailable_incident_snapshot(
                limit=bounded,
                detail="live incident store could not provide a snapshot",
            )

    @app.get(
        "/api/incidents/{incident_id}/action",
        tags=["actions"],
        response_model=ActionControlResponse,
    )
    async def action_control(
        incident_id: str,
        response: Response,
    ) -> ActionControlResponse:
        """The latest immutable server-held plan revision for one incident."""
        store: ActionControlStore | None = getattr(
            app.state,
            "action_control_store",
            None,
        )
        if store is None:
            response.status_code = 503
            return unavailable_action_control(
                status="degraded",
                message="no action-control store is attached to this gateway",
            )
        try:
            control = await store.get_action_control(incident_id)
            if control is None:
                response.status_code = 404
                return unavailable_action_control(
                    status="not_found",
                    message=f"no action plan exists for incident {incident_id}",
                )
            return action_control_response(control)
        except Exception:
            LOGGER.exception("action-control read failed")
            response.status_code = 503
            return unavailable_action_control(
                status="degraded",
                message="the action-control store could not provide a snapshot",
            )

    @app.post(
        "/api/incidents/{incident_id}/action",
        tags=["actions"],
        response_model=ActionControlResponse,
    )
    async def mutate_action_control(
        incident_id: str,
        request: ActionControlRequest,
        response: Response,
    ) -> ActionControlResponse:
        """Record intent only; the exact action remains the server-held plan."""
        if request.incident_id != incident_id:
            response.status_code = 400
            return unavailable_action_control(
                status="degraded",
                message="the request incident must match the route incident",
            )
        store: ActionControlStore | None = getattr(
            app.state,
            "action_control_store",
            None,
        )
        if store is None:
            response.status_code = 503
            return unavailable_action_control(
                status="degraded",
                message="no action-control store is attached to this gateway",
            )
        try:
            changed, control = await store.transition_action_control(
                request,
                actor=config.interim_operator_id,
                ts=datetime.now(UTC),
            )
            if changed:
                app.state.stream_broker.publish(
                    snapshot_invalidation(
                        SnapshotResource.INCIDENTS,
                        SnapshotResource.ACTIONS,
                        SnapshotResource.SECURITY,
                    )
                )
            return action_control_response(control)
        except ActionControlNotFoundError:
            response.status_code = 404
            return unavailable_action_control(
                status="not_found",
                message=(
                    f"no action plan exists for incident {incident_id} "
                    f"at revision {request.plan_revision}"
                ),
            )
        except ActionControlTransitionError as exc:
            response.status_code = 409
            return unavailable_action_control(status="degraded", message=str(exc))
        except Exception:
            LOGGER.exception("action-control transition failed")
            response.status_code = 503
            return unavailable_action_control(
                status="degraded",
                message="the action-control store could not commit the requested intent",
            )

    @app.get(
        "/api/incidents/{incident_id}",
        tags=["incidents"],
        response_model=IncidentDetailResponse,
    )
    async def incident_detail(
        incident_id: str,
        response: Response,
    ) -> IncidentDetailResponse:
        """The evidence-complete durable proof for one stable incident id."""
        reader: IncidentDetailReader | None = getattr(
            app.state,
            "incident_detail_reader",
            None,
        )
        if reader is None:
            response.status_code = 503
            return unavailable_incident_detail(
                status="degraded",
                message="no incident detail store is attached to this gateway",
            )
        try:
            record = await reader.get_incident_detail(incident_id)
            if record is None:
                response.status_code = 404
                return unavailable_incident_detail(
                    status="not_found",
                    message=f"no durable proof exists for incident {incident_id}",
                )
            return incident_detail_snapshot(record)
        except IncidentDetailDataError as exc:
            response.status_code = 503
            return unavailable_incident_detail(status="degraded", message=str(exc))
        except Exception:
            LOGGER.exception("incident detail read failed")
            response.status_code = 503
            return unavailable_incident_detail(
                status="degraded",
                message="incident detail store could not provide a proof snapshot",
            )

    @app.get(
        "/api/causal-graph",
        tags=["incidents"],
        response_model=CausalGraphResponse,
    )
    async def causal_graph(response: Response) -> CausalGraphResponse:
        """The evidence-backed topology for the newest unresolved incident."""
        reader: CausalGraphReader | None = getattr(
            app.state,
            "causal_graph_reader",
            None,
        )
        if reader is None:
            response.status_code = 503
            return unavailable_causal_graph(
                detail="no causal graph store is attached to this gateway"
            )
        try:
            return causal_graph_snapshot(await reader.latest_incident_graph())
        except CausalGraphDataError as exc:
            response.status_code = 503
            return unavailable_causal_graph(detail=str(exc))
        except Exception:
            LOGGER.exception("causal graph snapshot read failed")
            response.status_code = 503
            return unavailable_causal_graph(
                detail="causal graph store could not provide a snapshot"
            )

    @app.get(
        "/api/security",
        tags=["security"],
        response_model=SecurityResponse,
    )
    async def security(response: Response) -> SecurityResponse:
        """The newest unresolved incident's evidence-only security projection."""
        reader: SecuritySnapshotReader | None = getattr(
            app.state,
            "security_reader",
            None,
        )
        if reader is None:
            response.status_code = 503
            return unavailable_security_snapshot(
                detail="no security snapshot store is attached to this gateway"
            )
        try:
            record = await reader.latest_security_snapshot()
            control = (
                None if record is None else await reader.get_action_control(record.incident_id)
            )
            return security_snapshot(record, action_control=control)
        except SecuritySnapshotDataError as exc:
            response.status_code = 503
            return unavailable_security_snapshot(detail=str(exc))
        except Exception:
            LOGGER.exception("security snapshot read failed")
            response.status_code = 503
            return unavailable_security_snapshot(
                detail="security snapshot store could not provide a trusted response"
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
