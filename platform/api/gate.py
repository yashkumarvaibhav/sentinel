"""Interim access gate.

A stopgap, and labelled as one. Until real identity lands (OIDC + RBAC), the
public deployment still must not let an anonymous visitor *change* anything, so
mutating and explicitly-sensitive requests carry a shared secret. Read-only
requests — health, version, and the telemetry the command centre renders — stay
open, because there is nothing to protect yet and a login wall over a dashboard
is friction without safety.

This is not authentication. It answers "may this request act", not "who is
this", and it is replaced wholesale in Phase 8.1.
"""

from __future__ import annotations

import hmac

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response
from starlette.types import ASGIApp

SECRET_HEADER = "x-sentinel-secret"

# Methods that change state always pass through the gate. GET/HEAD/OPTIONS are
# read-only and open.
_MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


class SharedSecretGate(BaseHTTPMiddleware):
    """Require a shared secret on mutating and sensitive requests.

    With no secret configured the gate is inert — every request passes — so
    local development and the zero-config stack are unaffected. It only bites
    once a secret is set, which is exactly when the app is about to be exposed.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        secret: str,
        sensitive_prefixes: tuple[str, ...] = (),
    ) -> None:
        super().__init__(app)
        self._secret = secret
        self._sensitive_prefixes = sensitive_prefixes

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if not self._secret:
            return await call_next(request)

        if self._requires_secret(request) and not self._authorized(request):
            return JSONResponse(
                status_code=401,
                content={"detail": "this request requires the shared secret"},
            )

        return await call_next(request)

    def _requires_secret(self, request: Request) -> bool:
        if request.method in _MUTATING_METHODS:
            return True
        path = request.url.path
        return any(path.startswith(prefix) for prefix in self._sensitive_prefixes)

    def _authorized(self, request: Request) -> bool:
        presented = request.headers.get(SECRET_HEADER, "")
        # Constant-time comparison: a timing side channel on a shared secret is
        # a real leak, cheap to avoid.
        return bool(presented) and hmac.compare_digest(presented, self._secret)
