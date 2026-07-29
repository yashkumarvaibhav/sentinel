"""Delayed action settlement reads telemetry and never repeats an effect."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import cast

import httpx
import pytest

from action.rollback import (
    SloReading,
    SloSettlementVerifier,
    VictoriaMetricsSloReader,
)
from action.runtime import ActionControlRuntime
from common.config import ServiceSlo, SloConfig
from contracts import (
    ActionControlSnapshot,
    ActionSloSampleStatus,
    RollbackVerificationStatus,
)

TS = datetime(2026, 7, 29, 9, 0, tzinfo=UTC)


@dataclass
class _ScriptedReader:
    readings: dict[tuple[str, datetime], SloReading | None]

    def __call__(self, service: str, *, ts: datetime) -> SloReading | None:
        return self.readings[(service, ts)]


def _slos() -> SloConfig:
    return SloConfig(
        version=1,
        slos=(
            ServiceSlo(
                service="checkout",
                availability_target=0.995,
                latency_p95_ms=1000.0,
                evaluation_window_minutes=1440,
            ),
        ),
    )


def test_rollback_settlement_compares_the_same_slo_before_and_after() -> None:
    after = TS + timedelta(seconds=30)
    verifier = SloSettlementVerifier(
        slos=_slos(),
        reader=_ScriptedReader(
            {
                ("checkout", TS): SloReading(
                    service="checkout",
                    availability=0.90,
                    latency_p95_ms=1200.0,
                ),
                ("checkout", after): SloReading(
                    service="checkout",
                    availability=0.999,
                    latency_p95_ms=300.0,
                ),
            }
        ),
    )

    before = verifier.capture(ts=TS)
    proof = verifier.verify_rollback(before=before, ts=after)

    assert proof.status is RollbackVerificationStatus.VERIFIED
    assert proof.users_restored == pytest.approx(0.099)
    assert proof.before == before
    assert proof.after[0].availability == 0.999
    assert proof.checked_signals == ("availability", "latency_p95_ms")


def test_rollback_settlement_fails_closed_when_one_read_is_missing() -> None:
    after = TS + timedelta(seconds=30)
    verifier = SloSettlementVerifier(
        slos=_slos(),
        reader=_ScriptedReader(
            {
                ("checkout", TS): SloReading(
                    service="checkout",
                    availability=0.90,
                    latency_p95_ms=1200.0,
                ),
                ("checkout", after): None,
            }
        ),
    )

    proof = verifier.verify_rollback(before=verifier.capture(ts=TS), ts=after)

    assert proof.status is RollbackVerificationStatus.INSUFFICIENT
    assert proof.users_restored is None
    assert proof.after[0].status is ActionSloSampleStatus.INSUFFICIENT
    assert "none is claimed" in proof.detail


def test_victoriametrics_reader_uses_server_span_telemetry_for_both_signals() -> None:
    queries: list[str] = []

    def answer(request: httpx.Request) -> httpx.Response:
        query = request.url.params["query"]
        queries.append(query)
        value = "0.9995" if query.startswith("1 -") else "432.5"
        return httpx.Response(
            200,
            json={
                "status": "success",
                "data": {
                    "resultType": "vector",
                    "result": [{"metric": {}, "value": [TS.timestamp(), value]}],
                },
            },
        )

    with httpx.Client(
        transport=httpx.MockTransport(answer),
        base_url="http://victoriametrics:8428",
    ) as client:
        reader = VictoriaMetricsSloReader(client=client, window_seconds=300)
        reading = reader("checkout", ts=TS)

    assert reading == SloReading(
        service="checkout",
        availability=0.9995,
        latency_p95_ms=432.5,
    )
    assert len(queries) == 2
    assert all('service_name="checkout"' in query for query in queries)
    assert all("SPAN_KIND_SERVER" in query for query in queries)


def test_settlement_uses_configured_telemetry_service_without_renaming_the_slo() -> None:
    queried: list[str] = []

    def read(service: str, *, ts: datetime) -> SloReading:
        assert ts == TS
        queried.append(service)
        return SloReading(service=service, availability=0.9995, latency_p95_ms=40.0)

    verifier = SloSettlementVerifier(
        slos=SloConfig(
            version=1,
            slos=(
                ServiceSlo(
                    service="frontend",
                    telemetry_service="frontend-proxy",
                    availability_target=0.999,
                    latency_p95_ms=500.0,
                    evaluation_window_minutes=1440,
                ),
            ),
        ),
        reader=read,
    )

    sample = verifier.capture(ts=TS)

    assert queried == ["frontend-proxy"]
    assert sample[0].service == "frontend"
    assert sample[0].status is ActionSloSampleStatus.MEASURED


def test_polling_runtime_drains_only_its_bounded_batch() -> None:
    class Poller:
        def __init__(self) -> None:
            self.calls = 0

        async def run_once(self, *, ts: datetime) -> ActionControlSnapshot:
            del ts
            self.calls += 1
            return cast(ActionControlSnapshot, object())

    poller = Poller()
    runtime = ActionControlRuntime(
        poller=poller,
        poll_interval_seconds=1.0,
        batch_limit=3,
        clock=lambda: TS,
    )

    completed = asyncio.run(runtime.drain_once())

    assert completed == 3
    assert poller.calls == 3
