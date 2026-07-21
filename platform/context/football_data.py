"""Cached football-data.org v4 fixture connector."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from common.config import SportsConnectorConfig
from contracts import ContextWindow

type MatchStatus = Literal[
    "SCHEDULED",
    "TIMED",
    "IN_PLAY",
    "PAUSED",
    "EXTRA_TIME",
    "PENALTY_SHOOTOUT",
    "FINISHED",
    "SUSPENDED",
    "POSTPONED",
    "CANCELLED",
    "AWARDED",
]
type CacheKey = tuple[str, date, date]

_CONTEXT_STATUSES = frozenset(
    {"SCHEDULED", "TIMED", "IN_PLAY", "PAUSED", "EXTRA_TIME", "PENALTY_SHOOTOUT", "FINISHED"}
)


class _WireModel(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)


class _Team(_WireModel):
    name: str = Field(min_length=1, max_length=255)


class _Competition(_WireModel):
    name: str = Field(min_length=1, max_length=255)
    code: str = Field(min_length=2, max_length=16)


class _Match(_WireModel):
    id: int
    utc_date: datetime = Field(alias="utcDate")
    status: MatchStatus
    competition: _Competition
    home_team: _Team = Field(alias="homeTeam")
    away_team: _Team = Field(alias="awayTeam")


class _MatchResponse(_WireModel):
    matches: tuple[_Match, ...]


@dataclass(frozen=True)
class ConnectorStats:
    requests: int
    cache_hits: int
    failures: int
    disabled: int


class FootballDataConnector:
    """Fetch official fixtures with bounded queries and a process-local response cache."""

    def __init__(
        self,
        *,
        client: httpx.AsyncClient,
        api_key: str,
        configuration: SportsConnectorConfig,
    ) -> None:
        self._client = client
        self._api_key = api_key.strip()
        self._configuration = configuration
        self._cache: dict[CacheKey, tuple[ContextWindow, ...]] = {}
        self._requests = 0
        self._cache_hits = 0
        self._failures = 0
        self._disabled = 0

    async def windows(self, *, as_of: datetime) -> tuple[ContextWindow, ...]:
        timestamp = _utc(as_of)
        if not self._configuration.enabled or not self._api_key:
            self._disabled += 1
            return ()

        date_from = timestamp.date() - timedelta(days=1)
        date_to = timestamp.date() + timedelta(days=2)
        windows: list[ContextWindow] = []
        for competition in self._configuration.competition_codes:
            windows.extend(await self._competition(competition, date_from, date_to))
        return tuple(
            sorted(
                (window for window in windows if window.valid_from <= timestamp < window.valid_to),
                key=lambda window: (window.valid_from, window.context_id),
            )
        )

    def stats(self) -> ConnectorStats:
        return ConnectorStats(
            requests=self._requests,
            cache_hits=self._cache_hits,
            failures=self._failures,
            disabled=self._disabled,
        )

    async def _competition(
        self, competition: str, date_from: date, date_to: date
    ) -> tuple[ContextWindow, ...]:
        key = (competition, date_from, date_to)
        cached = self._cache.get(key)
        if cached is not None:
            self._cache_hits += 1
            return cached

        self._requests += 1
        try:
            response = await self._client.get(
                f"/v4/competitions/{competition}/matches",
                params={"dateFrom": date_from.isoformat(), "dateTo": date_to.isoformat()},
                headers={"X-Auth-Token": self._api_key},
            )
            response.raise_for_status()
            document = _MatchResponse.model_validate_json(response.content)
            result = tuple(
                window for match in document.matches if (window := self._window(match)) is not None
            )
        except (httpx.HTTPError, ValidationError, ValueError):
            self._failures += 1
            result = ()
        self._cache[key] = result
        return result

    def _window(self, match: _Match) -> ContextWindow | None:
        if match.status not in _CONTEXT_STATUSES:
            return None
        kickoff = _utc(match.utc_date)
        return ContextWindow(
            context_id=f"football-data-match-{match.id}",
            name=(f"{match.home_team.name} vs {match.away_team.name} ({match.competition.name})"),
            event_type="sports_fixture",
            source="football-data.org",
            honesty="REAL",
            valid_from=kickoff - timedelta(minutes=self._configuration.lead_minutes),
            valid_to=kickoff + timedelta(minutes=self._configuration.duration_minutes),
            expected_delta=dict(self._configuration.expected_delta),
            trust_score=self._configuration.trust_score,
        )


def _utc(value: datetime) -> datetime:
    if value.utcoffset() != timedelta(0):
        raise ValueError("football-data fixture/query time must be timezone-aware UTC")
    return value.astimezone(UTC)
