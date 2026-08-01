"""The bus drives complete windows into judgement, and the offset moves last."""

from __future__ import annotations

import ast
import asyncio
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from api.incidents import IncidentPublishResult
from contracts import ContextWindow, Observation
from detection.watermark import WatermarkBuffer
from producer.service import NORMALIZED_TOPIC, BusRecord, LiveProducerService

START = datetime(2026, 7, 22, 12, 0, tzinfo=UTC)
REPO_ROOT = Path(__file__).resolve().parents[2]


@dataclass
class _Runtime:
    """A recording stand-in for the live decision runtime."""

    ticks: list[datetime] = field(default_factory=list)
    contexts: list[tuple[ContextWindow, ...]] = field(default_factory=list)
    counts: list[int] = field(default_factory=list)
    checkpoint: object = None
    fail_on: datetime | None = None

    anchor_ts: datetime = START
    base_tick_seconds: int = 2

    async def advance(
        self,
        *,
        observations: tuple[Observation, ...],
        tick_ts: datetime,
        contexts: tuple[ContextWindow, ...] = (),
    ) -> tuple[IncidentPublishResult, ...]:
        if self.fail_on is not None and tick_ts == self.fail_on:
            raise RuntimeError("storage is unavailable")
        self.ticks.append(tick_ts)
        self.contexts.append(contexts)
        self.counts.append(len(observations))
        return ()

    async def resume(self) -> object:
        return self.checkpoint


@dataclass
class _Committer:
    committed: list[int] = field(default_factory=list)

    async def commit(self, record: BusRecord) -> None:
        self.committed.append(record.offset)


# Frameworks the producer image deliberately does not install. A green local
# gate cannot see this: every extra is present in the development venv, and the
# first deploy is where the missing module surfaces.
_WEB_FRAMEWORKS = ("fastapi", "uvicorn", "starlette")


def test_the_producer_import_graph_never_reaches_a_web_framework() -> None:
    platform_root = REPO_ROOT / "platform"
    seen: set[str] = set()
    queue = deque(["producer.service", "producer.__main__"])
    violations: list[str] = []
    while queue:
        module = queue.popleft()
        if module in seen:
            continue
        seen.add(module)
        path = _module_path(platform_root, module)
        if path is None:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for imported in _imports(tree):
            if imported.split(".")[0] in _WEB_FRAMEWORKS:
                violations.append(f"{module} -> {imported}")
            queue.append(imported)

    assert violations == []
    assert "api.incidents" in seen, "the producer must reach the one publication seam"


def _module_path(root: Path, module: str) -> Path | None:
    candidate = root.joinpath(*module.split("."))
    if candidate.with_suffix(".py").is_file():
        return candidate.with_suffix(".py")
    if (candidate / "__init__.py").is_file():
        return candidate / "__init__.py"
    return None


def _imports(tree: ast.AST) -> list[str]:
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            found.append(node.module)
            found.extend(f"{node.module}.{alias.name}" for alias in node.names)
    return found


def test_a_record_is_only_committed_after_its_windows_are_judged() -> None:
    runtime = _Runtime()
    committer = _Committer()
    service = _service(runtime, committer)

    asyncio.run(
        _feed(service, [(0, START + timedelta(seconds=1)), (1, START + timedelta(seconds=15))])
    )

    assert runtime.ticks, "a closed window must be judged"
    assert committer.committed == [0, 1]


def test_a_window_that_cannot_be_judged_leaves_the_offset_where_it_was() -> None:
    runtime = _Runtime(fail_on=START + timedelta(seconds=2))
    committer = _Committer()
    service = _service(runtime, committer)

    with pytest.raises(RuntimeError):
        asyncio.run(
            _feed(service, [(0, START + timedelta(seconds=1)), (1, START + timedelta(seconds=15))])
        )

    assert committer.committed == [0]


def test_an_undecodable_record_is_counted_and_stepped_over() -> None:
    runtime = _Runtime()
    committer = _Committer()
    service = _service(runtime, committer)

    async def drive() -> None:
        await service.handle(BusRecord(topic=NORMALIZED_TOPIC, partition=0, offset=0, value=b"{"))
        await service.handle(BusRecord(topic=NORMALIZED_TOPIC, partition=0, offset=1, value=None))

    asyncio.run(drive())

    assert service.stats().undecodable == 2
    assert service.stats().consumed == 0
    assert committer.committed == [0, 1]


@dataclass(frozen=True)
class _Checkpoint:
    anchor_ts: datetime
    tick_ts: datetime
    published_incidents: int = 0


def test_a_resumed_producer_continues_from_its_durable_position() -> None:
    runtime = _Runtime(
        checkpoint=_Checkpoint(anchor_ts=START, tick_ts=START + timedelta(seconds=10))
    )
    service = _service(runtime, _Committer())

    asyncio.run(service.resume())
    asyncio.run(
        _feed(
            service,
            [(0, START + timedelta(seconds=5)), (1, START + timedelta(seconds=27))],
            resume=False,
        )
    )

    assert runtime.ticks[0] == START + timedelta(seconds=12)


def test_a_runtime_built_on_a_different_anchor_refuses_to_resume() -> None:
    """The detector state is anchored at construction, so a mismatch is a bug.

    Left unchecked it feeds the processors windows from before their own
    origin, which is exactly what a restarted producer did on the testbed.
    """
    runtime = _Runtime(
        checkpoint=_Checkpoint(
            anchor_ts=START - timedelta(minutes=5),
            tick_ts=START + timedelta(seconds=10),
        )
    )
    service = _service(runtime, _Committer())

    with pytest.raises(ValueError, match="durable anchor"):
        asyncio.run(service.resume())


def test_only_the_context_windows_covering_a_tick_are_offered_to_it() -> None:
    runtime = _Runtime()
    window = ContextWindow(
        context_id="event-1",
        name="Cup final",
        event_type="sports_fixture",
        source="fixtures-api",
        honesty="REAL",
        valid_from=START + timedelta(seconds=100),
        valid_to=START + timedelta(seconds=200),
        expected_delta={"frontend.request_rate": 3.0},
        trust_score=0.9,
    )

    @dataclass
    class _Contexts:
        reads: int = 0

        async def active(self, *, as_of: datetime) -> tuple[ContextWindow, ...]:
            self.reads += 1
            return (window,)

    sources = _Contexts()
    service = _service(runtime, _Committer(), contexts=sources)

    asyncio.run(
        _feed(service, [(0, START + timedelta(seconds=1)), (1, START + timedelta(seconds=15))])
    )

    assert sources.reads == 1
    assert all(item == () for item in runtime.contexts)


def test_a_context_source_that_fails_does_not_stop_the_loop() -> None:
    runtime = _Runtime()

    class _Broken:
        async def active(self, *, as_of: datetime) -> tuple[ContextWindow, ...]:
            raise ConnectionError("the fixtures API is unreachable")

    service = _service(runtime, _Committer(), contexts=_Broken())

    asyncio.run(
        _feed(service, [(0, START + timedelta(seconds=1)), (1, START + timedelta(seconds=15))])
    )

    assert runtime.ticks


def _service(
    runtime: _Runtime,
    committer: _Committer,
    *,
    contexts: object | None = None,
) -> LiveProducerService:
    return LiveProducerService(
        runtime=runtime,  # type: ignore[arg-type]
        buffer=WatermarkBuffer(tick_seconds=2, lateness_seconds=10, capacity=1_000),
        committer=committer,
        contexts=contexts,  # type: ignore[arg-type]
    )


async def _feed(
    service: LiveProducerService,
    records: list[tuple[int, datetime]],
    *,
    resume: bool = True,
) -> None:
    if resume:
        await service.resume()
    for offset, ts in records:
        await service.handle(
            BusRecord(
                topic=NORMALIZED_TOPIC,
                partition=0,
                offset=offset,
                value=_observation(f"o{offset}", ts).model_dump_json().encode(),
            )
        )


def _observation(observation_id: str, ts: datetime) -> Observation:
    return Observation(
        observation_id=observation_id,
        ts=ts,
        service="frontend-proxy",
        signal="span.duration_ms",
        value=10.0,
        unit="ms",
        attributes={"span.kind": 2},
        trace_refs=(f"trace-{observation_id}",),
    )
