"""Authoritative action-control read and transition boundaries."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from contracts import (
    ActionControlRequest,
    ActionControlResponse,
    ActionControlSnapshot,
)


class ActionControlStore(Protocol):
    """The durable state machine used by the protected REST routes."""

    async def get_action_control(
        self,
        incident_id: str,
        *,
        plan_revision: int | None = None,
    ) -> ActionControlSnapshot | None: ...

    async def transition_action_control(
        self,
        request: ActionControlRequest,
        *,
        actor: str,
        ts: datetime,
    ) -> tuple[bool, ActionControlSnapshot]: ...


def action_control_response(control: ActionControlSnapshot) -> ActionControlResponse:
    """Expose only a strictly validated authoritative snapshot."""
    return ActionControlResponse(status="ready", control=control, message=None)


def unavailable_action_control(
    *,
    status: str,
    message: str,
) -> ActionControlResponse:
    """Keep missing and degraded state distinct from a ready empty plan."""
    if status not in {"not_found", "degraded"}:
        raise ValueError("unavailable action status must be not_found or degraded")
    return ActionControlResponse.model_validate(
        {"status": status, "control": None, "message": message}
    )
