"""Deterministic composition of versioned and connected context sources."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Protocol

import httpx

from common.config import EventCalendarConfig
from common.settings import Settings
from context.football_data import FootballDataConnector
from contracts import ContextWindow


class ContextSource(Protocol):
    async def windows(self, *, as_of: datetime) -> tuple[ContextWindow, ...]: ...


class CalendarContextSource:
    """Map enabled operator calendar entries to typed context evidence."""

    def __init__(self, calendar: EventCalendarConfig) -> None:
        self._calendar = calendar

    async def windows(self, *, as_of: datetime) -> tuple[ContextWindow, ...]:
        timestamp = _utc(as_of)
        return tuple(
            ContextWindow(
                context_id=f"calendar-{event.event_id}",
                name=event.name,
                event_type=event.event_type,
                source=event.source,
                honesty=event.honesty,
                valid_from=event.valid_from,
                valid_to=event.valid_to,
                expected_delta=dict(event.expected_delta),
                trust_score=event.trust_score,
            )
            for event in self._calendar.events
            if event.enabled and event.valid_from <= timestamp < event.valid_to
        )


class ContextIntelligenceService:
    """Return active windows without collapsing independent overlapping evidence."""

    def __init__(self, *, sources: tuple[ContextSource, ...]) -> None:
        self._sources = sources

    async def active(self, *, as_of: datetime) -> tuple[ContextWindow, ...]:
        timestamp = _utc(as_of)
        groups = await asyncio.gather(
            *(source.windows(as_of=timestamp) for source in self._sources)
        )
        windows = [window for group in groups for window in group]
        identifiers = [window.context_id for window in windows]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("context sources produced duplicate context ids")
        return tuple(sorted(windows, key=lambda window: (window.valid_from, window.context_id)))


def configured_context_service(
    *,
    calendar: EventCalendarConfig,
    runtime: Settings,
    client: httpx.AsyncClient,
) -> ContextIntelligenceService:
    """Wire versioned sources to environment-only credentials."""
    sources: list[ContextSource] = [CalendarContextSource(calendar)]
    if calendar.sports_connector is not None:
        sources.append(
            FootballDataConnector(
                client=client,
                api_key=runtime.football_data_api_key.get_secret_value(),
                configuration=calendar.sports_connector,
            )
        )
    return ContextIntelligenceService(sources=tuple(sources))


@asynccontextmanager
async def open_context_service(
    *, calendar: EventCalendarConfig, runtime: Settings
) -> AsyncIterator[ContextIntelligenceService]:
    """Own the bounded HTTP client used by configured external sources."""
    async with httpx.AsyncClient(
        base_url=runtime.football_data_base_url,
        timeout=runtime.context_http_timeout_seconds,
    ) as client:
        yield configured_context_service(calendar=calendar, runtime=runtime, client=client)


def _utc(value: datetime) -> datetime:
    if value.utcoffset() != timedelta(0):
        raise ValueError("context query time must be timezone-aware UTC")
    return value.astimezone(UTC)
