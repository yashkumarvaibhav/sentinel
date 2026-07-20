"""Health reporting: a failing dependency is named, not hidden."""

import asyncio

from api.health import Readiness, check_health, probe_component


async def ok() -> None:
    return None


async def boom() -> None:
    raise ConnectionRefusedError("connection refused")


async def hangs() -> None:
    await asyncio.sleep(10)


def test_all_components_ready() -> None:
    report = asyncio.run(check_health({"postgres": ok, "loki": ok}, timeout=1.0))

    assert report.status is Readiness.READY
    assert report.degraded == ()
    assert [c.name for c in report.components] == ["loki", "postgres"]
    assert all(c.latency_ms >= 0 for c in report.components)


def test_one_failure_degrades_the_platform_and_names_the_component() -> None:
    report = asyncio.run(check_health({"postgres": ok, "tempo": boom}, timeout=1.0))

    assert report.status is Readiness.DEGRADED
    assert report.degraded == ("tempo",)
    failed = next(c for c in report.components if c.name == "tempo")
    assert failed.detail is not None
    assert "connection refused" in failed.detail


def test_a_hanging_probe_times_out_rather_than_blocking() -> None:
    result = asyncio.run(probe_component("clickhouse", hangs, timeout=0.05))

    assert not result.ready
    assert result.detail == "timed out after 0.05s"
