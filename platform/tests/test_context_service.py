"""Context selection is event-time driven and preserves overlapping evidence."""

from __future__ import annotations

import asyncio
import inspect
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from pydantic import SecretStr

import context.football_data as football_data_module
import context.service as service_module
from common.config import CalendarEvent, EventCalendarConfig, SportsConnectorConfig
from common.settings import Settings
from context.service import (
    CalendarContextSource,
    ContextIntelligenceService,
    configured_context_service,
)

_START = datetime(2026, 7, 21, 18, 30, tzinfo=UTC)


def test_no_event_and_half_open_active_boundaries() -> None:
    source = CalendarContextSource(_calendar(_event("match", start=_START)))
    service = ContextIntelligenceService(sources=(source,))

    before = asyncio.run(service.active(as_of=_START - timedelta(microseconds=1)))
    at_start = asyncio.run(service.active(as_of=_START))
    at_end = asyncio.run(service.active(as_of=_START + timedelta(hours=4)))

    assert before == ()
    assert len(at_start) == 1
    assert at_start[0].context_id == "calendar-match"
    assert at_start[0].honesty == "SIMULATED"
    assert at_start[0].expected_delta == {"frontend.request_rate": 2.5}
    assert at_end == ()


def test_disabled_events_are_ignored_and_overlaps_remain_separate() -> None:
    calendar = _calendar(
        _event("late", start=_START + timedelta(minutes=15), trust=0.7),
        _event("early", start=_START, trust=0.9),
        _event("disabled", start=_START, enabled=False),
    )
    service = ContextIntelligenceService(sources=(CalendarContextSource(calendar),))

    active = asyncio.run(service.active(as_of=_START + timedelta(minutes=30)))

    assert [window.context_id for window in active] == ["calendar-early", "calendar-late"]
    assert [window.trust_score for window in active] == [0.9, 0.7]


def test_query_time_must_be_utc_even_when_no_event_would_match() -> None:
    service = ContextIntelligenceService(sources=(CalendarContextSource(_calendar()),))

    with pytest.raises(ValueError, match="UTC"):
        asyncio.run(service.active(as_of=datetime(2026, 7, 21, 18, 30)))


def test_zero_key_connector_falls_back_to_active_calendar_context() -> None:
    asyncio.run(_zero_key_case())


async def _zero_key_case() -> None:
    sports = SportsConnectorConfig(
        enabled=True,
        provider="football-data.org",
        competition_codes=("CL",),
        lead_minutes=30,
        duration_minutes=150,
        expected_delta={"frontend.request_rate": 2.25},
        trust_score=0.9,
    )
    calendar = EventCalendarConfig(
        version=1,
        events=(_event("local", start=_START),),
        sports_connector=sports,
    )

    def unexpected_request(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"zero-key connector called upstream: {request.url}")

    async with httpx.AsyncClient(
        base_url="https://api.football-data.org",
        transport=httpx.MockTransport(unexpected_request),
    ) as client:
        service = configured_context_service(
            calendar=calendar,
            runtime=Settings(FOOTBALL_DATA_API_KEY=SecretStr("")),
            client=client,
        )
        active = await service.active(as_of=_START)

    assert [window.context_id for window in active] == ["calendar-local"]


def test_context_path_contains_no_wall_clock_read() -> None:
    implementation = inspect.getsource(service_module) + inspect.getsource(football_data_module)

    assert "datetime.now" not in implementation
    assert "datetime.utcnow" not in implementation
    assert "time.time" not in implementation


def _calendar(*events: CalendarEvent) -> EventCalendarConfig:
    return EventCalendarConfig(version=1, events=events, sports_connector=None)


def _event(
    event_id: str,
    *,
    start: datetime,
    trust: float = 0.8,
    enabled: bool = True,
) -> CalendarEvent:
    return CalendarEvent(
        event_id=event_id,
        name=f"Event {event_id}",
        event_type="sports_fixture",
        source="operator_calendar",
        honesty="SIMULATED",
        enabled=enabled,
        valid_from=start,
        valid_to=start + timedelta(hours=4),
        expected_delta={"frontend.request_rate": 2.5},
        trust_score=trust,
    )
