from __future__ import annotations

from datetime import date, datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


Source = Literal["claude-code", "codex"]
EventType = Literal["user", "agent", "tool", "tool_result", "usage"]
ToolStatus = Literal[
    "unknown", "running", "succeeded", "failed", "interrupted", "canceled"
]


class NormalizedEvent(StrictModel):
    session_id: Annotated[str, Field(min_length=1, max_length=256)]
    native_session_id: UUID | None = None
    type: EventType
    role: Literal["user", "assistant"] | None = None
    created_at: datetime
    local_day: date
    tool_name: Annotated[str, Field(max_length=128)] | None = None
    tool_status: ToolStatus = "unknown"
    content_preview: Annotated[str, Field(max_length=8192)] | None = None
    session_title: Annotated[str, Field(max_length=500)] | None = None
    project_name: Annotated[str, Field(max_length=500)] | None = None
    model: Annotated[str, Field(max_length=200)] | None = None
    source_call_id: Annotated[str, Field(max_length=256)] | None = None
    tool_input_preview: Annotated[str, Field(max_length=8192)] | None = None
    tool_output_preview: Annotated[str, Field(max_length=8192)] | None = None
    exit_code: int | None = None
    duration_ms: Annotated[int, Field(ge=0)] | None = None
    token_input: Annotated[int, Field(ge=0)] | None = None
    token_output: Annotated[int, Field(ge=0)] | None = None
    token_cache_read: Annotated[int, Field(ge=0)] | None = None
    token_cache_write: Annotated[int, Field(ge=0)] | None = None
    token_thinking: Annotated[int, Field(ge=0)] | None = None
    usage_cumulative: bool = False
    truncated: bool = False
    source_bytes: Annotated[int, Field(ge=0)] = 0


class IngestRecord(StrictModel):
    source: Source
    source_file_id: Annotated[str, Field(min_length=32, max_length=128)]
    sequence: Annotated[int, Field(ge=0)]
    item_index: Annotated[int, Field(ge=0)] = 0
    revision: Annotated[int, Field(ge=1)]
    stage: Literal["skeleton", "enriched"] = "skeleton"
    payload_hash: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    event: NormalizedEvent


class IngestBatch(StrictModel):
    protocol_version: Literal[1]
    batch_id: UUID
    device_sequence: Annotated[int, Field(ge=1)]
    extractor_version: Annotated[int, Field(ge=1)]
    records: Annotated[list[IngestRecord], Field(min_length=1, max_length=500)]


class IngestReceipt(StrictModel):
    batch_id: UUID
    accepted: int
    duplicate: int
    rejected: int


class ClaimResponse(StrictModel):
    claim_token: str
    expires_at: datetime


class ExchangeClaimRequest(StrictModel):
    claim_token: Annotated[str, Field(min_length=32, max_length=256)]
    device_name: Annotated[str, Field(min_length=1, max_length=120)]
    platform: Annotated[str, Field(min_length=1, max_length=64)]


class ExchangeClaimResponse(StrictModel):
    device_id: UUID
    device_token: str


class DevicePrincipal(StrictModel):
    device_id: UUID
    user_id: UUID


class DeviceStatus(StrictModel):
    device_id: UUID
    pending_supported: bool = True


class BrowserDevice(StrictModel):
    id: UUID
    name: str
    platform: str
    extractor_version: int
    created_at: datetime
    last_seen_at: datetime | None = None
    last_upload_at: datetime | None = None
    status: Literal["connected", "revoked", "expired"]
    sessions: int = 0


class DashboardEvent(StrictModel):
    t: int
    d: date
    src: Source
    s: str
    k: Literal["user", "agent", "tool"]
    st: ToolStatus
    n: str | None = None
    ms: int | None = None
    id: str


class DashboardSession(StrictModel):
    id: str
    src: Source
    title: str
    proj: str
    model: str | None = None
    start: int
    end: int
    d: date
    summary: str | None = None
    summary_state: Literal["ready", "pending", "not_requested", "failed"]


class DashboardTool(StrictModel):
    name: str
    count: int
    ok: int
    fail: int
    p50: int
    p90: int
    max: int


class DashboardRollup(StrictModel):
    sessions: int
    events: int
    tools: int
    ok: int
    fail: int


class DashboardTokens(StrictModel):
    input: int = Field(serialization_alias="in")
    out: int
    cr: int
    cw: int
    th: int


class AgentTokens(DashboardTokens):
    total: int


class EventDetail(StrictModel):
    id: str
    content: str | None = None
    tool_input: str | None = None
    tool_output: str | None = None
    truncated: bool = False


class DashboardStory(StrictModel):
    t: int
    d: date
    s: str
    src: Source
    k: Literal["user", "agent"]
    x: str


class DashboardStats(StrictModel):
    files: int
    corpus_gb: float
    strokes: int


class DashboardPayload(StrictModel):
    generated: datetime
    rollups: dict[str, dict[Source, DashboardRollup]]
    sessions: list[DashboardSession]
    events: list[DashboardEvent]
    tools: list[DashboardTool]
    tokens: dict[str, DashboardTokens]
    tokens_by_source: dict[str, dict[Source, AgentTokens]] = Field(default_factory=dict)
    story: list[DashboardStory]
    stats: DashboardStats


class SummaryRequestResponse(StrictModel):
    session_id: str
    state: Literal["ready", "pending"]


class DayRevisions(StrictModel):
    ribbon: str
    extras: str
    story: str
    purge: str


class CalendarPayload(StrictModel):
    contract_version: Literal[1] = 1
    day_revisions: dict[str, DayRevisions] = Field(default_factory=dict)
    generated: datetime
    rollups: dict[str, dict[Source, DashboardRollup]]
    revision: int = 0
    rollups_pending: bool = False
    refresh_after_ms: int = 30000


class DayPayload(StrictModel):
    sessions: list[DashboardSession]
    events: list[DashboardEvent]
    tools: list[DashboardTool]
    story: list[DashboardStory]
    tokens: dict[str, DashboardTokens]
    tokens_by_source: dict[str, dict[Source, AgentTokens]] = Field(default_factory=dict)


class PublicConfig(StrictModel):
    supabase_url: str
    supabase_publishable_key: str
