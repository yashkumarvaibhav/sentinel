"""The decomposition read behind the command centre's hero chart."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from api.app import create_app
from api.decomposition import DEFAULT_WINDOW, MAX_WINDOW, DecompositionRangeError, resolve_window
from common.settings import Settings
from contracts import DecompFrame

NOW = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)


def _frame(offset: int, *, residual: float = 0.0) -> DecompFrame:
    base = 100.0
    event = 40.0
    return DecompFrame(
        frame_id=f"frame-{offset}",
        observation_id=f"observation-{offset}",
        ts=NOW + timedelta(seconds=offset),
        service="frontend",
        signal="ingress.requests",
        observed=base + event + residual,
        explained_base=base,
        explained_event=event,
        residual=residual,
        band_low=base + event - 10,
        band_high=base + event + 10,
        residual_score=min(residual / 100.0, 1.0),
    )


@dataclass
class RecordedReader:
    """A store that answers from a script and records what it was asked."""

    frames: tuple[DecompFrame, ...] = ()
    calls: list[dict[str, object]] = field(default_factory=list)

    async def list_decomp_frames(
        self,
        *,
        service: str,
        signal: str,
        start: datetime,
        end: datetime,
        limit: int = 1_000,
    ) -> tuple[DecompFrame, ...]:
        self.calls.append(
            {"service": service, "signal": signal, "start": start, "end": end, "limit": limit}
        )
        return self.frames


def _client(reader: RecordedReader | None) -> Iterator[TestClient]:
    app = create_app(
        config=Settings(),
        probes={},
        decomposition_reader=reader,
    )
    with TestClient(app) as client:
        yield client


@pytest.fixture
def reader() -> RecordedReader:
    return RecordedReader(frames=(_frame(0), _frame(2, residual=25.0)))


# --- the window ---------------------------------------------------------------


def test_a_window_defaults_to_the_last_half_hour() -> None:
    start, end = resolve_window(None, None, now=NOW)

    assert end == NOW
    assert end - start == DEFAULT_WINDOW


def test_an_offsetless_timestamp_is_refused_rather_than_assumed() -> None:
    """An offsetless timestamp means different moments to the client and the store."""
    naive = datetime(2026, 3, 1, 11, 0)

    with pytest.raises(DecompositionRangeError, match="timezone"):
        resolve_window(naive, None, now=NOW)


def test_a_window_wider_than_the_cap_is_refused() -> None:
    with pytest.raises(DecompositionRangeError, match="at most"):
        resolve_window(NOW - MAX_WINDOW - timedelta(seconds=1), NOW, now=NOW)


def test_a_backwards_window_is_refused() -> None:
    with pytest.raises(DecompositionRangeError, match="at or after"):
        resolve_window(NOW, NOW - timedelta(minutes=1), now=NOW)


# --- the endpoint -------------------------------------------------------------


def test_the_frames_are_returned_whole_so_the_identity_can_be_checked(
    reader: RecordedReader,
) -> None:
    """A server that returned only the residual would ask to be trusted on arithmetic."""
    client = next(_client(reader))

    body = client.get(
        "/api/decomposition", params={"service": "frontend", "signal": "ingress.requests"}
    ).json()

    assert body["count"] == 2
    for frame in body["frames"]:
        assert (
            pytest.approx(frame["observed"])
            == frame["explained_base"] + frame["explained_event"] + frame["residual"]
        )


def test_an_empty_window_is_an_answer_not_a_missing_route() -> None:
    client = next(_client(RecordedReader(frames=())))

    response = client.get(
        "/api/decomposition", params={"service": "cart", "signal": "ingress.requests"}
    )

    assert response.status_code == 200
    assert response.json()["count"] == 0
    assert response.json()["frames"] == []


def test_a_truncated_window_says_so(reader: RecordedReader) -> None:
    """A client that cannot tell it was truncated will draw a partial chart as whole."""
    client = next(_client(reader))

    body = client.get(
        "/api/decomposition",
        params={"service": "frontend", "signal": "ingress.requests", "limit": 2},
    ).json()

    assert body["truncated"] is True
    assert body["limit"] == 2


def test_a_full_window_is_not_reported_as_truncated() -> None:
    client = next(_client(RecordedReader(frames=(_frame(0),))))

    body = client.get(
        "/api/decomposition",
        params={"service": "frontend", "signal": "ingress.requests", "limit": 100},
    ).json()

    assert body["truncated"] is False


def test_an_impossible_window_is_refused_with_its_reason(reader: RecordedReader) -> None:
    client = next(_client(reader))

    response = client.get(
        "/api/decomposition",
        params={
            "service": "frontend",
            "signal": "ingress.requests",
            "start": "2026-03-01T12:00:00Z",
            "end": "2026-03-01T11:00:00Z",
        },
    )

    assert response.status_code == 400
    assert "at or after" in response.json()["detail"]
    assert reader.calls == [], "a refused window must never reach the store"


def test_no_store_attached_answers_503_rather_than_an_empty_chart(
    reader: RecordedReader,
) -> None:
    """ "No store" and "nothing happened" must not look the same.

    The state is cleared after startup rather than by passing ``None`` to the
    factory, because the factory no longer has that meaning: a gateway with no
    reader injected builds its own ClickHouse one. What this guards is the
    genuinely degraded case - the attribute absent because the store could not
    be reached - and setting it directly is the only honest way to reach it.
    """
    client = next(_client(reader))
    client.app.state.decomposition_reader = None  # type: ignore[attr-defined]

    response = client.get(
        "/api/decomposition", params={"service": "frontend", "signal": "ingress.requests"}
    )

    assert response.status_code == 503
    assert response.json()["frames"] == []
    assert reader.calls == []


def test_the_read_is_bounded_before_it_reaches_the_store(reader: RecordedReader) -> None:
    client = next(_client(reader))

    client.get(
        "/api/decomposition",
        params={"service": "frontend", "signal": "ingress.requests", "limit": 1_000_000},
    )

    assert reader.calls[0]["limit"] == 5_000
