"""Record one contained live scenario at exact raw-topic offset boundaries."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import json
import os
import re
import secrets
import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import cast

from aiokafka import AIOKafkaConsumer
from aiokafka.admin import AIOKafkaAdminClient

from common.config import load_config
from lab.captures.broker import (
    BoundedConsumer,
    BrokerSnapshot,
    bounds_between,
    read_bounded_records,
    snapshot_offsets,
)
from lab.captures.models import CaptureSeedPurpose, CaptureTelemetry
from lab.captures.store import CaptureMetadata, load_runtime_capture, write_capture
from lab.captures.transcript import replay_decomposition
from lab.scenarios import SeedPurpose, compile_profile, load_profile, write_artifacts
from lab.scoring.live import run_live_scenario

_SAFE_ID = re.compile(r"^[a-z0-9](?:[-a-z0-9]{0,126}[a-z0-9])?$")
_EMPTY_PHASE_ONE_ENRICHMENT = b'{"items":[],"version":1}\n'


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m lab.captures")
    subparsers = parser.add_subparsers(dest="command", required=True)
    record = subparsers.add_parser("record", help="record one committed live scenario seed")
    record.add_argument("--repo-root", type=Path, required=True)
    record.add_argument(
        "--profile", choices=("quiet_day", "match_night", "attack_day"), required=True
    )
    record.add_argument("--seed", type=int, required=True)
    record.add_argument("--purpose", choices=("development", "held_out"), default="held_out")
    record.add_argument("--capture-id", required=True)
    record.add_argument("--output", type=Path, required=True)
    replay = subparsers.add_parser("replay", help="write one canonical decomposition transcript")
    replay.add_argument("--repo-root", type=Path, required=True)
    replay.add_argument("--capture", type=Path, required=True)
    replay.add_argument("--transcript", type=Path, required=True)
    subparsers.add_parser("_snapshot", help=argparse.SUPPRESS)
    collect = subparsers.add_parser("_collect", help=argparse.SUPPRESS)
    collect.add_argument("--before", type=Path, required=True)
    collect.add_argument("--after", type=Path, required=True)
    collect.add_argument("--schedule", type=Path, required=True)
    collect.add_argument("--telemetry", type=Path, required=True)
    collect.add_argument("--context", type=Path, required=True)
    collect.add_argument("--labels", type=Path, required=True)
    collect.add_argument("--output", type=Path, required=True)
    collect.add_argument("--capture-id", required=True)
    collect.add_argument("--scenario-id", required=True)
    collect.add_argument("--seed", type=int, required=True)
    collect.add_argument("--seed-purpose", choices=("development", "held_out"), required=True)
    collect.add_argument("--config-fingerprint", required=True)
    collect.add_argument("--correlation-user-agent", required=True)
    collect.add_argument("--anchor-user-agent", required=True)
    args = parser.parse_args(argv)
    if args.command == "record":
        return _record(args)
    if args.command == "replay":
        return _replay(args)
    if args.command == "_snapshot":
        return asyncio.run(_print_snapshot())
    if args.command == "_collect":
        return asyncio.run(_collect(args))
    raise AssertionError(f"unhandled command: {args.command}")


def _record(args: argparse.Namespace) -> int:
    repo_root = args.repo_root.resolve()
    output = args.output.resolve()
    capture_id = _safe_id(args.capture_id, "capture ID")
    _relative_to_var(repo_root, output)
    if output.exists() and any(output.iterdir()):
        raise ValueError(f"capture directory is not empty: {output}")
    profile = load_profile(repo_root / "lab" / "scenarios" / f"{args.profile}.yml")
    artifacts = compile_profile(profile, seed=args.seed, purpose=SeedPurpose(args.purpose))
    invocation = secrets.token_hex(4)
    work = repo_root / "var" / "capture-work" / f"{capture_id}-{invocation}"
    paths = write_artifacts(artifacts, work)
    telemetry_path = work / "telemetry.json"
    telemetry_path.write_bytes(_canonical(profile.telemetry.model_dump(mode="json")))

    print(f"[capture] {capture_id}: snapshotting raw-topic starts", flush=True)
    before = _container_snapshot(repo_root)
    telemetry = run_live_scenario(
        repo_root=repo_root,
        artifacts=artifacts,
        invocation=invocation,
    )
    print(f"[capture] {capture_id}: snapshotting raw-topic ends", flush=True)
    after = _container_snapshot(repo_root)
    before_path = work / "before.json"
    after_path = work / "after.json"
    before_path.write_bytes(_canonical(before.model_dump(mode="json")))
    after_path.write_bytes(_canonical(after.model_dump(mode="json")))
    config = load_config(repo_root / "config")
    command = _compose_capture_command(repo_root)
    command.extend(
        [
            "_collect",
            "--before",
            _container_path(repo_root, before_path),
            "--after",
            _container_path(repo_root, after_path),
            "--schedule",
            _container_path(repo_root, paths.schedule),
            "--telemetry",
            _container_path(repo_root, telemetry_path),
            "--context",
            _container_path(repo_root, paths.context_feed),
            "--labels",
            _container_path(repo_root, paths.labels),
            "--output",
            _container_path(repo_root, output),
            "--capture-id",
            capture_id,
            "--scenario-id",
            artifacts.schedule.scenario_id,
            "--seed",
            str(artifacts.schedule.seed),
            "--seed-purpose",
            artifacts.schedule.seed_purpose.value,
            "--config-fingerprint",
            config.fingerprint,
            "--correlation-user-agent",
            telemetry.correlation_user_agent,
            "--anchor-user-agent",
            telemetry.anchor_user_agent,
        ]
    )
    print(f"[capture] {capture_id}: draining exact raw-topic ranges", flush=True)
    summary = _run(command).strip()
    print(f"[capture] {capture_id}: {summary}", flush=True)
    return 0


def _replay(args: argparse.Namespace) -> int:
    repo_root = args.repo_root.resolve()
    transcript_path = args.transcript.resolve()
    _relative_to_var(repo_root, transcript_path)
    config = load_config(repo_root / "config")
    replay = replay_decomposition(
        load_runtime_capture(args.capture.resolve()),
        detector=config.detectors,
        replay_config_fingerprint=config.fingerprint,
    )
    transcript = replay.canonical_bytes()
    transcript_path.parent.mkdir(parents=True, exist_ok=True)
    transcript_path.write_bytes(transcript)
    print(
        json.dumps(
            {
                "capture_id": replay.capture_id,
                "decomposed": replay.stats.decomposed,
                "raw_dead_letters": len(replay.raw_dead_letters),
                "telemetry_completeness": replay.telemetry_completeness,
                "ticks": len(replay.steps),
                "transcript_sha256": hashlib.sha256(transcript).hexdigest(),
            },
            separators=(",", ":"),
            sort_keys=True,
        )
    )
    return 0


async def _print_snapshot() -> int:
    consumer = _consumer()
    admin = _admin()
    await consumer.start()  # type: ignore[no-untyped-call]
    await admin.start()  # type: ignore[no-untyped-call]
    try:
        snapshot = await snapshot_offsets(
            cast(BoundedConsumer, consumer),
            _AdminCatalog(admin),
        )
    finally:
        await _close_admin(admin)
        await _stop_consumer(consumer)
    print(_canonical(snapshot.model_dump(mode="json")).decode(), end="")
    return 0


async def _collect(args: argparse.Namespace) -> int:
    before = BrokerSnapshot.model_validate_json(args.before.read_bytes())
    after = BrokerSnapshot.model_validate_json(args.after.read_bytes())
    bounds = bounds_between(before, after)
    consumer = _consumer()
    await consumer.start()  # type: ignore[no-untyped-call]
    try:
        records = await read_bounded_records(
            cast(BoundedConsumer, consumer),
            bounds,
            timeout_seconds=60,
        )
    finally:
        await _stop_consumer(consumer)
    manifest = write_capture(
        args.output,
        metadata=CaptureMetadata(
            capture_id=_safe_id(args.capture_id, "capture ID"),
            scenario_id=args.scenario_id,
            seed=args.seed,
            seed_purpose=cast(CaptureSeedPurpose, args.seed_purpose),
            telemetry_honesty="REAL",
            stimulus_honesty="SIMULATED",
            config_fingerprint=args.config_fingerprint,
            correlation_user_agent=args.correlation_user_agent,
            anchor_user_agent=args.anchor_user_agent,
            telemetry=CaptureTelemetry.model_validate_json(args.telemetry.read_bytes()),
        ),
        topic_bounds=bounds,
        records=records,
        schedule=args.schedule.read_bytes(),
        context_feed=args.context.read_bytes(),
        private_labels=args.labels.read_bytes(),
        enrichments={"phase-1.json": _EMPTY_PHASE_ONE_ENRICHMENT},
    )
    size_bytes = sum(item.size_bytes for topic in manifest.topics for item in topic.records)
    print(
        json.dumps(
            {
                "capture_id": manifest.capture_id,
                "raw_bytes": size_bytes,
                "records": sum(len(topic.records) for topic in manifest.topics),
            },
            separators=(",", ":"),
            sort_keys=True,
        )
    )
    return 0


def _container_snapshot(repo_root: Path) -> BrokerSnapshot:
    command = _compose_capture_command(repo_root)
    command.append("_snapshot")
    return BrokerSnapshot.model_validate_json(_run(command))


def _compose_capture_command(repo_root: Path) -> list[str]:
    return [
        "docker",
        "compose",
        "--project-name",
        "sentinel",
        "--project-directory",
        str(repo_root),
        "-f",
        str(repo_root / "deploy" / "docker-compose.yml"),
        "run",
        "--rm",
        "--no-deps",
        "-T",
        "--user",
        f"{os.getuid()}:{os.getgid()}",
        "--volume",
        f"{repo_root / 'lab'}:/app/lab:ro",
        "--volume",
        f"{repo_root / 'var'}:/sentinel-var",
        "ingest",
        "python",
        "-m",
        "lab.captures",
    ]


def _consumer() -> AIOKafkaConsumer:
    return AIOKafkaConsumer(  # type: ignore[no-untyped-call]
        bootstrap_servers=os.environ.get("REDPANDA_BROKERS", "redpanda:9092"),
        enable_auto_commit=False,
        group_id=None,
        auto_offset_reset="earliest",
    )


def _admin() -> AIOKafkaAdminClient:
    return AIOKafkaAdminClient(
        bootstrap_servers=os.environ.get("REDPANDA_BROKERS", "redpanda:9092")
    )


class _AdminCatalog:
    def __init__(self, admin: AIOKafkaAdminClient) -> None:
        self._admin = admin

    async def topic_partitions(self, topic: str) -> set[int] | None:
        documents = await self._admin.describe_topics([topic])
        if len(documents) != 1 or not isinstance(documents[0], dict):
            raise RuntimeError(f"broker returned invalid topic metadata: {topic}")
        document = documents[0]
        if document.get("error_code") != 0 or document.get("topic") != topic:
            return None
        raw_partitions = document.get("partitions")
        if not isinstance(raw_partitions, list):
            raise RuntimeError(f"broker omitted topic partitions: {topic}")
        partitions: set[int] = set()
        for raw_partition in raw_partitions:
            if not isinstance(raw_partition, dict):
                raise RuntimeError(f"broker returned invalid partition metadata: {topic}")
            partition = raw_partition.get("partition")
            if not isinstance(partition, int) or partition < 0:
                raise RuntimeError(f"broker returned invalid partition ID: {topic}")
            partitions.add(partition)
        return partitions


async def _stop_consumer(consumer: AIOKafkaConsumer) -> None:
    # aiokafka 0.14 can surface cancellation from its metadata-reset task when
    # startup failed; do not hide the recorder's original fail-closed error.
    with contextlib.suppress(asyncio.CancelledError):
        await consumer.stop()  # type: ignore[no-untyped-call]


async def _close_admin(admin: AIOKafkaAdminClient) -> None:
    with contextlib.suppress(asyncio.CancelledError):
        await admin.close()  # type: ignore[no-untyped-call]


def _container_path(repo_root: Path, path: Path) -> str:
    relative = _relative_to_var(repo_root, path.resolve())
    return str(Path("/sentinel-var") / relative)


def _relative_to_var(repo_root: Path, path: Path) -> Path:
    try:
        return path.relative_to((repo_root / "var").resolve())
    except ValueError as exc:
        raise ValueError("capture work and output must stay under repo/var") from exc


def _safe_id(value: str, field: str) -> str:
    if _SAFE_ID.fullmatch(value) is None:
        raise ValueError(f"unsafe {field}: {value!r}")
    return value


def _run(command: list[str]) -> str:
    completed = subprocess.run(
        command,
        text=True,
        capture_output=True,
        check=False,
        timeout=180,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise RuntimeError(f"capture command failed ({command[0]}): {detail}")
    return completed.stdout


def _canonical(value: object) -> bytes:
    return (
        json.dumps(value, allow_nan=False, separators=(",", ":"), sort_keys=True) + "\n"
    ).encode()


if __name__ == "__main__":
    raise SystemExit(main())
