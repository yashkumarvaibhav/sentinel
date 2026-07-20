"""Build identity of the running process.

The gateway serves this at ``/api/version`` and the web footer shows it, so that
"which code is actually live?" is answerable from the browser. Values are baked
into the image at build time via environment variables; outside a built image
they fall back to ``unknown`` rather than shelling out to git — the running
service must never depend on a working tree being present.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass

UNKNOWN = "unknown"

_SHA_ENV = "SENTINEL_GIT_SHA"
_BUILT_AT_ENV = "SENTINEL_BUILT_AT"
_VERSION_ENV = "SENTINEL_VERSION"


@dataclass(frozen=True, slots=True)
class BuildInfo:
    """Identity of this build: release version, source commit, build timestamp."""

    version: str
    git_sha: str
    built_at: str

    @property
    def short_sha(self) -> str:
        """First 7 characters of the commit SHA, or ``unknown``."""
        return self.git_sha[:7] if self.git_sha != UNKNOWN else UNKNOWN

    @property
    def is_stamped(self) -> bool:
        """True when the build carries a real commit stamp (i.e. it was built, not run raw)."""
        return self.git_sha != UNKNOWN


def build_info(env: Mapping[str, str] | None = None) -> BuildInfo:
    """Read the build stamp from the environment.

    Blank or missing variables are normalized to ``unknown`` so callers never
    have to distinguish "unset" from "set to empty string".
    """
    source = os.environ if env is None else env
    return BuildInfo(
        version=_read(source, _VERSION_ENV),
        git_sha=_read(source, _SHA_ENV),
        built_at=_read(source, _BUILT_AT_ENV),
    )


def _read(source: Mapping[str, str], key: str) -> str:
    value = source.get(key, "").strip()
    return value or UNKNOWN
