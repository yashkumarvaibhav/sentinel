"""Context plane: event calendars, connectors, source trust, expected bands."""

from context.football_data import FootballDataConnector
from context.service import (
    CalendarContextSource,
    ContextIntelligenceService,
    configured_context_service,
    open_context_service,
)

__all__ = [
    "CalendarContextSource",
    "ContextIntelligenceService",
    "FootballDataConnector",
    "configured_context_service",
    "open_context_service",
]
