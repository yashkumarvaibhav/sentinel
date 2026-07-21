"""Write and verify capture files without exposing private labels to runtime."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal

from lab.captures.models import (
    CaptureManifest,
    CaptureTelemetry,
    PrivateArtifact,
    PublicArtifact,
    RawRecordDescriptor,
    TopicCapture,
)

type RawTopic = Literal["otlp.raw.metrics", "otlp.raw.logs", "otlp.raw.traces"]
_ENRICHMENT_NAME = re.compile(r"^[A-Za-z0-9_.-]+$")


@dataclass(frozen=True)
class CaptureMetadata:
    capture_id: str
    scenario_id: str
    seed: int
    seed_purpose: Literal["held_out"]
    telemetry_honesty: Literal["REAL"]
    stimulus_honesty: Literal["SIMULATED"]
    config_fingerprint: str
    correlation_user_agent: str
    anchor_user_agent: str
    telemetry: CaptureTelemetry


@dataclass(frozen=True)
class TopicBounds:
    topic: RawTopic
    partition: int
    start_offset: int
    end_offset: int


@dataclass(frozen=True)
class CaptureSourceRecord:
    topic: RawTopic
    partition: int
    offset: int
    value: bytes


@dataclass(frozen=True)
class RuntimeCapture:
    root: Path
    manifest: CaptureManifest
    records: tuple[CaptureSourceRecord, ...]
    schedule: bytes
    context_feed: bytes
    enrichments: tuple[tuple[str, bytes], ...]


def write_capture(
    root: Path,
    *,
    metadata: CaptureMetadata,
    topic_bounds: tuple[TopicBounds, ...],
    records: tuple[CaptureSourceRecord, ...],
    schedule: bytes,
    context_feed: bytes,
    private_labels: bytes,
    enrichments: dict[str, bytes],
) -> CaptureManifest:
    if root.exists() and any(root.iterdir()):
        raise ValueError(f"capture directory is not empty: {root}")
    root.mkdir(parents=True, exist_ok=True)
    bounds_by_source = {(item.topic, item.partition): item for item in topic_bounds}
    if len(bounds_by_source) != len(topic_bounds):
        raise ValueError("topic bounds must be unique")
    grouped: dict[tuple[str, int], list[CaptureSourceRecord]] = {
        source: [] for source in bounds_by_source
    }
    for record in records:
        source = (record.topic, record.partition)
        if source not in grouped:
            raise ValueError(f"raw record has no declared topic bounds: {source}")
        grouped[source].append(record)
    topics: list[TopicCapture] = []
    for source, bounds in sorted(bounds_by_source.items()):
        descriptors: list[RawRecordDescriptor] = []
        ordered = sorted(grouped[source], key=lambda item: item.offset)
        for record in ordered:
            relative = f"raw/{record.topic}/{record.partition}/{record.offset}.otlp"
            path = _owned_path(root, relative)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(record.value)
            descriptors.append(
                RawRecordDescriptor(
                    offset=record.offset,
                    path=relative,
                    size_bytes=len(record.value),
                    sha256=_sha256(record.value),
                )
            )
        topics.append(
            TopicCapture(
                topic=bounds.topic,
                partition=bounds.partition,
                start_offset=bounds.start_offset,
                end_offset=bounds.end_offset,
                records=tuple(descriptors),
            )
        )
    schedule_artifact = _write_public(root, "schedule", "public/schedule.json", schedule)
    context_artifact = _write_public(root, "context-feed", "public/context-feed.json", context_feed)
    public_enrichments = tuple(
        _write_public(root, name, f"public/enrichment/{name}", value)
        for name, value in sorted(enrichments.items())
    )
    labels_path = "private/labels.json"
    _owned_path(root, labels_path).parent.mkdir(parents=True, exist_ok=True)
    _owned_path(root, labels_path).write_bytes(private_labels)
    manifest = CaptureManifest(
        version=1,
        **metadata.__dict__,
        topics=tuple(topics),
        schedule=schedule_artifact,
        context_feed=context_artifact,
        enrichments=public_enrichments,
        private_labels=PrivateArtifact(path=labels_path, sha256=_sha256(private_labels)),
    )
    (root / "manifest.json").write_bytes(_canonical(manifest.model_dump(mode="json")))
    return manifest


def load_runtime_capture(root: Path) -> RuntimeCapture:
    manifest = _load_manifest(root)
    schedule = _verified(root, manifest.schedule.path, manifest.schedule.sha256)
    context_feed = _verified(root, manifest.context_feed.path, manifest.context_feed.sha256)
    enrichments = tuple(
        (artifact.name, _verified(root, artifact.path, artifact.sha256))
        for artifact in manifest.enrichments
    )
    records: list[CaptureSourceRecord] = []
    for topic in manifest.topics:
        for record in topic.records:
            value = _verified(root, record.path, record.sha256, error="raw payload checksum")
            if len(value) != record.size_bytes:
                raise ValueError(f"raw payload size mismatch: {record.path}")
            records.append(
                CaptureSourceRecord(
                    topic=topic.topic,
                    partition=topic.partition,
                    offset=record.offset,
                    value=value,
                )
            )
    return RuntimeCapture(
        root=root,
        manifest=manifest,
        records=tuple(records),
        schedule=schedule,
        context_feed=context_feed,
        enrichments=enrichments,
    )


def load_private_labels(root: Path) -> bytes:
    manifest = _load_manifest(root)
    return _verified(
        root,
        manifest.private_labels.path,
        manifest.private_labels.sha256,
        error="private labels checksum",
    )


def _load_manifest(root: Path) -> CaptureManifest:
    try:
        return CaptureManifest.model_validate_json((root / "manifest.json").read_bytes())
    except (OSError, ValueError) as exc:
        raise ValueError(f"invalid capture manifest: {exc}") from exc


def _write_public(root: Path, name: str, relative: str, value: bytes) -> PublicArtifact:
    if _ENRICHMENT_NAME.fullmatch(name) is None:
        raise ValueError(f"invalid public artifact name: {name!r}")
    path = _owned_path(root, relative)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(value)
    return PublicArtifact(name=name, path=relative, sha256=_sha256(value))


def _verified(
    root: Path, relative: str, checksum: str, *, error: str = "artifact checksum"
) -> bytes:
    try:
        value = _owned_path(root, relative).read_bytes()
    except OSError as exc:
        raise ValueError(f"capture artifact missing: {relative}") from exc
    if _sha256(value) != checksum:
        raise ValueError(f"{error} mismatch: {relative}")
    return value


def _owned_path(root: Path, relative: str) -> Path:
    candidate = PurePosixPath(relative)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise ValueError(f"capture path escapes root: {relative}")
    return root.joinpath(*candidate.parts)


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _canonical(value: object) -> bytes:
    return (
        json.dumps(value, allow_nan=False, separators=(",", ":"), sort_keys=True) + "\n"
    ).encode()
