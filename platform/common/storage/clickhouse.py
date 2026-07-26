"""Async ClickHouse repository for full-resolution telemetry and decomposition."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import cast

import httpx

from common.storage._clickhouse import encode_json_each_row, execute, rows
from contracts import DecompFrame, Observation, Symptom, SymptomKind

_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


class ClickHouseRepository:
    """Persist retry-safe high-volume evidence through ClickHouse's HTTP interface."""

    def __init__(self, *, client: httpx.AsyncClient, database: str) -> None:
        if _IDENTIFIER.fullmatch(database) is None:
            raise ValueError("invalid ClickHouse database identifier")
        self._client = client
        self._database = database

    async def write_observations(self, records: Sequence[Observation]) -> None:
        """Insert observations; stable ids collapse retries when queried with FINAL."""
        encoded = [
            {
                "observation_id": record.observation_id,
                "ts": _format_timestamp(record.ts),
                "service": record.service,
                "signal": record.signal,
                "value": record.value,
                "unit": record.unit,
                "attributes_json": json.dumps(
                    record.attributes,
                    allow_nan=False,
                    separators=(",", ":"),
                    sort_keys=True,
                ),
                "flow_refs": list(record.flow_refs),
                "log_refs": list(record.log_refs),
                "trace_refs": list(record.trace_refs),
            }
            for record in records
        ]
        await self._insert(
            "observations",
            (
                "observation_id",
                "ts",
                "service",
                "signal",
                "value",
                "unit",
                "attributes_json",
                "flow_refs",
                "log_refs",
                "trace_refs",
            ),
            encoded,
        )

    async def write_decomp_frames(self, records: Sequence[DecompFrame]) -> None:
        """Insert full decomposition frames without discarding explained components."""
        encoded = [
            {
                "frame_id": record.frame_id,
                "observation_id": record.observation_id,
                "ts": _format_timestamp(record.ts),
                "service": record.service,
                "signal": record.signal,
                "observed": record.observed,
                "explained_base": record.explained_base,
                "explained_event": record.explained_event,
                "residual": record.residual,
                "band_low": record.band_low,
                "band_high": record.band_high,
                "residual_score": record.residual_score,
                "context_ids": list(record.context_ids),
            }
            for record in records
        ]
        await self._insert(
            "decomp_frames",
            (
                "frame_id",
                "observation_id",
                "ts",
                "service",
                "signal",
                "observed",
                "explained_base",
                "explained_event",
                "residual",
                "band_low",
                "band_high",
                "residual_score",
                "context_ids",
            ),
            encoded,
        )

    async def write_symptoms(self, records: Sequence[Symptom]) -> None:
        """Insert deterministic symptom evidence for downstream verification."""
        encoded = [
            {
                "symptom_id": record.symptom_id,
                "kind": record.kind.value,
                "service": record.service,
                "signal": record.signal,
                "onset_ts": _format_timestamp(record.onset_ts),
                "score": record.score,
                "note": record.note,
                "evidence_refs": list(record.evidence_refs),
            }
            for record in records
        ]
        await self._insert(
            "symptoms",
            (
                "symptom_id",
                "kind",
                "service",
                "signal",
                "onset_ts",
                "score",
                "note",
                "evidence_refs",
            ),
            encoded,
        )

    async def get_observation(self, observation_id: str) -> Observation | None:
        """Read the latest stored version of one observation."""
        result = await rows(
            self._client,
            f"""
            SELECT
                observation_id,
                toUnixTimestamp64Micro(ts) AS ts_us,
                service,
                signal,
                value,
                unit,
                attributes_json,
                flow_refs,
                log_refs,
                trace_refs
            FROM {self._database}.observations FINAL
            WHERE observation_id = {{record_id:String}}
            LIMIT 1
            FORMAT JSONEachRow
            """,
            parameters={"record_id": observation_id},
        )
        return _observation(result[0]) if result else None

    async def get_decomp_frame(self, frame_id: str) -> DecompFrame | None:
        """Read the latest stored version of one decomposition frame."""
        result = await rows(
            self._client,
            f"""
            SELECT
                frame_id,
                observation_id,
                toUnixTimestamp64Micro(ts) AS ts_us,
                service,
                signal,
                observed,
                explained_base,
                explained_event,
                residual,
                band_low,
                band_high,
                residual_score,
                context_ids
            FROM {self._database}.decomp_frames FINAL
            WHERE frame_id = {{record_id:String}}
            LIMIT 1
            FORMAT JSONEachRow
            """,
            parameters={"record_id": frame_id},
        )
        return _decomp_frame(result[0]) if result else None

    async def get_symptom(self, symptom_id: str) -> Symptom | None:
        """Read the latest stored version of one deterministic symptom."""
        result = await rows(
            self._client,
            f"""
            SELECT
                symptom_id,
                kind,
                service,
                signal,
                toUnixTimestamp64Micro(onset_ts) AS ts_us,
                score,
                note,
                evidence_refs
            FROM {self._database}.symptoms FINAL
            WHERE symptom_id = {{record_id:String}}
            LIMIT 1
            FORMAT JSONEachRow
            """,
            parameters={"record_id": symptom_id},
        )
        return _symptom(result[0]) if result else None

    async def list_observations(
        self,
        *,
        service: str,
        signal: str,
        start: datetime,
        end: datetime,
        limit: int = 1_000,
    ) -> tuple[Observation, ...]:
        """Read a bounded event-time window in deterministic order."""
        if start.utcoffset() is None or end.utcoffset() is None:
            raise ValueError("observation range must use timezone-aware timestamps")
        if end < start:
            raise ValueError("end must be greater than or equal to start")
        if not 1 <= limit <= 10_000:
            raise ValueError("limit must be between 1 and 10000")
        result = await rows(
            self._client,
            f"""
            SELECT
                observation_id,
                toUnixTimestamp64Micro(ts) AS ts_us,
                service,
                signal,
                value,
                unit,
                attributes_json,
                flow_refs,
                log_refs,
                trace_refs
            FROM {self._database}.observations FINAL
            WHERE service = {{service:String}}
              AND signal = {{signal:String}}
              AND toUnixTimestamp64Micro(ts) >= {{start_us:Int64}}
              AND toUnixTimestamp64Micro(ts) <= {{end_us:Int64}}
            ORDER BY ts, observation_id
            LIMIT {limit}
            FORMAT JSONEachRow
            """,
            parameters={
                "service": service,
                "signal": signal,
                "start_us": _timestamp_microseconds(start),
                "end_us": _timestamp_microseconds(end),
            },
        )
        return tuple(_observation(row) for row in result)

    async def list_decomp_frames(
        self,
        *,
        service: str,
        signal: str,
        start: datetime,
        end: datetime,
        limit: int = 1_000,
    ) -> tuple[DecompFrame, ...]:
        """Read a bounded window of decomposition frames in event-time order.

        The read behind the command centre's hero chart. It is bounded the same
        way ``list_observations`` is, and for the same reason: a full-resolution
        window is exactly the shape of request that lets anybody who can reach
        the gateway make it do arbitrary work.

        ``FINAL`` matters more here than for observations. A frame is rewritten
        when a later context window changes what the world explains about a tick
        already recorded, and a chart drawn from both versions would show one
        moment twice with two different residuals - the one number this product
        exists to be trusted about.
        """
        if start.utcoffset() is None or end.utcoffset() is None:
            raise ValueError("decomposition range must use timezone-aware timestamps")
        if end < start:
            raise ValueError("end must be greater than or equal to start")
        if not 1 <= limit <= 10_000:
            raise ValueError("limit must be between 1 and 10000")
        result = await rows(
            self._client,
            f"""
            SELECT
                frame_id,
                observation_id,
                toUnixTimestamp64Micro(ts) AS ts_us,
                service,
                signal,
                observed,
                explained_base,
                explained_event,
                residual,
                band_low,
                band_high,
                residual_score,
                context_ids
            FROM {self._database}.decomp_frames FINAL
            WHERE service = {{service:String}}
              AND signal = {{signal:String}}
              AND toUnixTimestamp64Micro(ts) >= {{start_us:Int64}}
              AND toUnixTimestamp64Micro(ts) <= {{end_us:Int64}}
            ORDER BY ts, frame_id
            LIMIT {limit}
            FORMAT JSONEachRow
            """,
            parameters={
                "service": service,
                "signal": signal,
                "start_us": _timestamp_microseconds(start),
                "end_us": _timestamp_microseconds(end),
            },
        )
        return tuple(_decomp_frame(row) for row in result)

    async def _insert(
        self,
        table: str,
        columns: tuple[str, ...],
        records: Sequence[Mapping[str, object]],
    ) -> None:
        if not records:
            return
        names = ", ".join(columns)
        query = f"INSERT INTO {self._database}.{table} ({names}) FORMAT JSONEachRow"
        await execute(self._client, query, body=encode_json_each_row(records))


def _observation(row: Mapping[str, object]) -> Observation:
    attributes = json.loads(_string(row, "attributes_json"))
    if not isinstance(attributes, dict):
        raise RuntimeError("stored observation attributes are not a JSON object")
    return Observation(
        observation_id=_string(row, "observation_id"),
        ts=_timestamp(row),
        service=_string(row, "service"),
        signal=_string(row, "signal"),
        value=_float(row, "value"),
        unit=_string(row, "unit"),
        attributes=cast(dict[str, str | bool | int | float], attributes),
        flow_refs=_strings(row, "flow_refs"),
        log_refs=_strings(row, "log_refs"),
        trace_refs=_strings(row, "trace_refs"),
    )


def _decomp_frame(row: Mapping[str, object]) -> DecompFrame:
    return DecompFrame(
        frame_id=_string(row, "frame_id"),
        observation_id=_string(row, "observation_id"),
        ts=_timestamp(row),
        service=_string(row, "service"),
        signal=_string(row, "signal"),
        observed=_float(row, "observed"),
        explained_base=_float(row, "explained_base"),
        explained_event=_float(row, "explained_event"),
        residual=_float(row, "residual"),
        band_low=_float(row, "band_low"),
        band_high=_float(row, "band_high"),
        residual_score=_float(row, "residual_score"),
        context_ids=_strings(row, "context_ids"),
    )


def _symptom(row: Mapping[str, object]) -> Symptom:
    return Symptom(
        symptom_id=_string(row, "symptom_id"),
        kind=SymptomKind(_string(row, "kind")),
        service=_string(row, "service"),
        signal=_string(row, "signal"),
        onset_ts=_timestamp(row),
        score=_float(row, "score"),
        note=_string(row, "note"),
        evidence_refs=_strings(row, "evidence_refs"),
    )


def _string(row: Mapping[str, object], field: str) -> str:
    value = row.get(field)
    if not isinstance(value, str):
        raise RuntimeError(f"stored {field} is not a string")
    return value


def _float(row: Mapping[str, object], field: str) -> float:
    value = row.get(field)
    if not isinstance(value, int | float) or isinstance(value, bool):
        raise RuntimeError(f"stored {field} is not numeric")
    return float(value)


def _strings(row: Mapping[str, object], field: str) -> tuple[str, ...]:
    value = row.get(field)
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise RuntimeError(f"stored {field} is not a string array")
    return tuple(cast(list[str], value))


def _timestamp(row: Mapping[str, object]) -> datetime:
    value = row.get("ts_us")
    if isinstance(value, str) and value.isdigit():
        value = int(value)
    if not isinstance(value, int) or isinstance(value, bool):
        raise RuntimeError("stored timestamp is not integer microseconds")
    return _EPOCH + timedelta(microseconds=value)


def _timestamp_microseconds(value: datetime) -> int:
    if value.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    delta = value.astimezone(UTC) - _EPOCH
    return (delta.days * 86_400 + delta.seconds) * 1_000_000 + delta.microseconds


def _format_timestamp(value: datetime) -> str:
    if value.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return value.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S.%f")
