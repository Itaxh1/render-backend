from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Protocol
from uuid import UUID, uuid4

from .models import (
    BrowserDevice,
    CalendarPayload,
    DayPayload,
    EventDetail,
    DashboardEvent,
    DashboardPayload,
    DashboardRollup,
    DashboardSession,
    DashboardStats,
    DashboardTokens,
    DevicePrincipal,
    IngestBatch,
    IngestReceipt,
)
from .usage import aggregate_usage


def token_hash(token: str) -> bytes:
    return hashlib.sha256(token.encode("utf-8")).digest()


class InvalidClaimError(Exception):
    pass


class BatchConflictError(Exception):
    pass


class InvalidDeviceError(Exception):
    pass


class Store(Protocol):
    async def open(self) -> None: ...
    async def close(self) -> None: ...
    async def ready(self) -> bool: ...
    async def create_claim(self, user_id: UUID) -> tuple[str, datetime]: ...
    async def exchange_claim(
        self, claim_token: str, device_name: str, platform: str
    ) -> tuple[UUID, str]: ...
    async def authenticate_device(self, device_token: str) -> DevicePrincipal | None: ...
    async def list_devices(self, user_id: UUID) -> list[BrowserDevice]: ...
    async def revoke_device(self, user_id: UUID, device_id: UUID) -> bool: ...
    async def calendar(self, user_id: UUID, year: int) -> CalendarPayload: ...
    async def day_detail(self, user_id: UUID, day: date) -> DayPayload: ...
    async def event_detail(self, user_id: UUID, event_id: str) -> EventDetail | None: ...
    async def ingest(
        self, principal: DevicePrincipal, batch: IngestBatch
    ) -> IngestReceipt: ...
    async def dashboard(
        self, user_id: UUID, year: int, day: date | None = None
    ) -> DashboardPayload: ...
    async def request_summary(self, user_id: UUID, session_id: int) -> bool: ...


@dataclass
class _Claim:
    user_id: UUID
    expires_at: datetime
    consumed: bool = False


class MemoryStore:
    """Test store with the same ownership and idempotency rules as Postgres."""

    def __init__(self) -> None:
        self._claims: dict[bytes, _Claim] = {}
        self._devices: dict[bytes, DevicePrincipal] = {}
        self._device_details: dict[UUID, BrowserDevice] = {}
        self._events: dict[tuple[UUID, str, int, int], int] = {}
        self._records: dict[tuple[UUID, str, int, int], tuple[DevicePrincipal, object]] = {}
        self._receipts: dict[tuple[UUID, UUID], tuple[bytes, IngestReceipt]] = {}
        self._device_sequences: dict[tuple[UUID, int], UUID] = {}
        self._lock = asyncio.Lock()

    async def open(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def ready(self) -> bool:
        return True

    async def create_claim(self, user_id: UUID) -> tuple[str, datetime]:
        token = secrets.token_urlsafe(32)
        expires_at = datetime.now(timezone.utc) + timedelta(minutes=10)
        async with self._lock:
            self._claims[token_hash(token)] = _Claim(user_id, expires_at)
        return token, expires_at

    async def exchange_claim(
        self, claim_token: str, device_name: str, platform: str
    ) -> tuple[UUID, str]:
        async with self._lock:
            claim = self._claims.get(token_hash(claim_token))
            if (
                claim is None
                or claim.consumed
                or claim.expires_at <= datetime.now(timezone.utc)
            ):
                raise InvalidClaimError
            claim.consumed = True
            device_id = uuid4()
            device_token = secrets.token_urlsafe(32)
            self._devices[token_hash(device_token)] = DevicePrincipal(
                device_id=device_id, user_id=claim.user_id
            )
            self._device_details[device_id] = BrowserDevice(
                id=device_id, name=device_name, platform=platform,
                extractor_version=1, created_at=datetime.now(timezone.utc),
                status="connected",
            )
            return device_id, device_token

    async def authenticate_device(self, device_token: str) -> DevicePrincipal | None:
        principal = self._devices.get(token_hash(device_token))
        if principal is None:
            return None
        detail = self._device_details[principal.device_id]
        if detail.status != "connected":
            return None
        detail.last_seen_at = datetime.now(timezone.utc)
        return principal

    async def list_devices(self, user_id: UUID) -> list[BrowserDevice]:
        result = []
        for principal in self._devices.values():
            if principal.user_id != user_id:
                continue
            detail = self._device_details[principal.device_id].model_copy()
            detail.sessions = len({
                (record.source, record.event.session_id)
                for owner, record in self._records.values()
                if owner.device_id == principal.device_id and owner.user_id == user_id
            })
            result.append(detail)
        return sorted(result, key=lambda item: item.created_at, reverse=True)

    async def revoke_device(self, user_id: UUID, device_id: UUID) -> bool:
        async with self._lock:
            if not any(p.user_id == user_id and p.device_id == device_id
                       for p in self._devices.values()):
                return False
            self._device_details[device_id].status = "revoked"
            return True

    async def ingest(
        self, principal: DevicePrincipal, batch: IngestBatch
    ) -> IngestReceipt:
        receipt_key = (principal.device_id, batch.batch_id)
        request_hash = hashlib.sha256(
            json.dumps(
                batch.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
        ).digest()
        async with self._lock:
            detail = self._device_details.get(principal.device_id)
            if detail is None or detail.status != "connected":
                raise InvalidDeviceError
            existing_receipt = self._receipts.get(receipt_key)
            if existing_receipt is not None:
                previous_hash, receipt = existing_receipt
                if previous_hash != request_hash:
                    raise BatchConflictError
                return receipt
            sequence_key = (principal.device_id, batch.device_sequence)
            previous_batch = self._device_sequences.get(sequence_key)
            if previous_batch is not None and previous_batch != batch.batch_id:
                raise BatchConflictError
            accepted = 0
            duplicate = 0
            for record in batch.records:
                key = (
                    principal.device_id,
                    record.source_file_id,
                    record.sequence,
                    record.item_index,
                )
                previous_revision = self._events.get(key)
                if previous_revision is not None and previous_revision >= record.revision:
                    duplicate += 1
                    continue
                self._events[key] = record.revision
                self._records[key] = (principal, record)
                accepted += 1
            receipt = IngestReceipt(
                batch_id=batch.batch_id,
                accepted=accepted,
                duplicate=duplicate,
                rejected=0,
            )
            self._receipts[receipt_key] = (request_hash, receipt)
            self._device_sequences[sequence_key] = batch.batch_id
            detail.last_upload_at = datetime.now(timezone.utc)
            detail.extractor_version = max(detail.extractor_version, batch.extractor_version)
            return receipt

    async def calendar(self, user_id: UUID, year: int) -> CalendarPayload:
        data = await self.dashboard(user_id, year)
        return CalendarPayload(generated=data.generated, rollups=data.rollups)

    async def day_detail(self, user_id: UUID, day: date) -> DayPayload:
        data = await self.dashboard(user_id, day.year, day)
        return DayPayload(**data.model_dump(include={"sessions", "events", "tools", "story", "tokens", "tokens_by_source"}))

    async def event_detail(self, user_id: UUID, event_id: str) -> EventDetail | None:
        for principal, record in self._records.values():
            if principal.user_id != user_id or record.event.type not in {"user", "agent", "tool"}:
                continue
            if f"{record.source_file_id}:{record.sequence}:{record.item_index}" == event_id:
                event = record.event
                return EventDetail(id=event_id, content=event.content_preview,
                    tool_input=event.tool_input_preview, tool_output=event.tool_output_preview,
                    truncated=event.truncated)
        return None

    async def dashboard(
        self, user_id: UUID, year: int, day: date | None = None
    ) -> DashboardPayload:
        visible = []
        sessions: dict[str, dict] = {}
        rolls: dict[str, dict] = {}
        usage_rows = []
        previous_usage = {}
        # Delta cumulative snapshots before filtering by year/day: the previous
        # sample can belong to yesterday or the previous year.
        for principal, record in sorted(self._records.values(),
                key=lambda pair: (pair[1].event.created_at, pair[1].sequence, pair[1].item_index)):
            event = record.event
            if principal.user_id != user_id or (event.token_input is None and event.token_output is None):
                continue
            key = (principal.device_id, record.source, event.session_id)
            raw = {field: getattr(event, field) or 0 for field in (
                "token_input", "token_output", "token_cache_read", "token_cache_write", "token_thinking")}
            previous = previous_usage.get(key, {})
            delta = {field: value - previous[field]
                     if event.usage_cumulative and field in previous and value >= previous[field]
                     else value for field, value in raw.items()}
            previous_usage[key] = raw
            if event.local_day.year == year and (day is None or event.local_day == day):
                usage_rows.append({"local_day": event.local_day, "source": record.source, **delta})
        tokens, tokens_by_source = aggregate_usage(usage_rows)
        files: set[str] = set()
        source_bytes = 0
        for principal, record in self._records.values():
            event = record.event
            if principal.user_id != user_id or event.local_day.year != year:
                continue
            files.add(record.source_file_id)
            source_bytes += event.source_bytes
            session_key = f"{principal.device_id}:{record.source}:{event.session_id}"
            session = sessions.setdefault(session_key, {
                "id": session_key,
                "src": record.source,
                "title": event.session_title or "Untitled session",
                "proj": event.project_name or "Unknown project",
                "model": event.model,
                "start": int(event.created_at.timestamp() * 1000),
                "end": int(event.created_at.timestamp() * 1000),
                "d": event.local_day,
            })
            timestamp = int(event.created_at.timestamp() * 1000)
            session["start"] = min(session["start"], timestamp)
            session["end"] = max(session["end"], timestamp)
            day_key = event.local_day.isoformat()
            if event.type not in {"user", "agent", "tool"}:
                continue
            status = event.tool_status if event.type == "tool" else "succeeded"
            visible.append(DashboardEvent(
                t=timestamp,
                d=event.local_day,
                src=record.source,
                s=session_key,
                k=event.type,
                st=status,
                n=event.tool_name,
                ms=event.duration_ms if record.source == "claude-code" else None,
                id=f"{record.source_file_id}:{record.sequence}:{record.item_index}",
            ))
            source_roll = rolls.setdefault(day_key, {}).setdefault(record.source, {
                "session_ids": set(), "events": 0, "tools": 0, "ok": 0, "fail": 0,
            })
            source_roll["session_ids"].add(session_key)
            source_roll["events"] += 1
            if event.type == "tool":
                source_roll["tools"] += 1
                if status == "succeeded": source_roll["ok"] += 1
                if status == "failed": source_roll["fail"] += 1
        normalized_rolls = {
            day: {
                source: DashboardRollup(
                    sessions=len(values.pop("session_ids")), **values
                )
                for source, values in sources.items()
            }
            for day, sources in rolls.items()
        }
        selected = day.isoformat() if day else max(rolls, default=f"{year}-01-01")
        total_strokes = len(visible)
        visible = [event for event in visible if event.d.isoformat() == selected]
        visible_sessions = {event.s for event in visible}
        return DashboardPayload(
            generated=datetime.now(timezone.utc),
            rollups=normalized_rolls,
            sessions=[
                DashboardSession(
                    **{**values, "d": date.fromisoformat(selected)},
                    summary=None,
                    summary_state="not_requested",
                )
                for key, values in sessions.items() if key in visible_sessions
            ],
            events=sorted(visible, key=lambda event: event.t),
            tools=[],
            tokens=tokens,
            tokens_by_source=tokens_by_source,
            story=[],
            stats=DashboardStats(
                files=len(files), corpus_gb=round(source_bytes / 1_000_000_000, 2),
                strokes=total_strokes,
            ),
        )

    async def request_summary(self, user_id: UUID, session_id: int) -> bool:
        del user_id, session_id
        return False
