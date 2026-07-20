"""Readiness of the things the platform depends on.

Health is reported per component, never as a single opaque boolean: an operator
looking at Sentinel during an incident needs to know *which* part of its own
pipeline is degraded, because that changes how much its verdicts can be
trusted. A probe that fails is recorded with its reason, not swallowed.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum

# A probe resolves if the component is reachable and raises otherwise. The
# exception message becomes the operator-visible detail.
Probe = Callable[[], Awaitable[None]]


class Readiness(StrEnum):
    """Overall readiness of the platform."""

    READY = "ready"
    DEGRADED = "degraded"


@dataclass(frozen=True, slots=True)
class ComponentHealth:
    """Result of probing one dependency."""

    name: str
    ready: bool
    latency_ms: float
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class HealthReport:
    """What `/api/health` answers with."""

    status: Readiness
    components: tuple[ComponentHealth, ...]

    @property
    def degraded(self) -> tuple[str, ...]:
        """Names of the components that failed their probe."""
        return tuple(c.name for c in self.components if not c.ready)


async def probe_component(name: str, probe: Probe, timeout: float) -> ComponentHealth:
    """Run one probe, converting any failure into a reported component state."""
    started = time.perf_counter()
    try:
        await asyncio.wait_for(probe(), timeout=timeout)
    except TimeoutError:
        return ComponentHealth(
            name=name,
            ready=False,
            latency_ms=_elapsed_ms(started),
            detail=f"timed out after {timeout:g}s",
        )
    except Exception as exc:  # any failure means "not ready" — reported with its reason
        return ComponentHealth(
            name=name, ready=False, latency_ms=_elapsed_ms(started), detail=_reason(exc)
        )
    return ComponentHealth(name=name, ready=True, latency_ms=_elapsed_ms(started))


async def check_health(probes: Mapping[str, Probe], timeout: float) -> HealthReport:
    """Probe every dependency concurrently and summarize."""
    results = await asyncio.gather(
        *(probe_component(name, probe, timeout) for name, probe in probes.items())
    )
    ordered = tuple(sorted(results, key=lambda c: c.name))
    status = Readiness.READY if all(c.ready for c in ordered) else Readiness.DEGRADED
    return HealthReport(status=status, components=ordered)


def _elapsed_ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 2)


def _reason(exc: Exception) -> str:
    text = str(exc).strip()
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__
