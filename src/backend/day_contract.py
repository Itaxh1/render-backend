"""Version-one response contract shared with Rexy fixtures (plan §7)."""
from datetime import date, datetime, time, timedelta
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field

from .models import AgentTokens, DashboardEvent, DashboardTokens, DayRevisions, Source, StrictModel


class Axis(StrictModel):
    timezone: str
    start_ms: int
    end_ms: int


def day_axis(day: date, tz: str) -> Axis:
    try:
        zone = ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError) as error:
        raise ValueError('Invalid IANA timezone') from error
    start = datetime.combine(day, time.min, zone)
    end = datetime.combine(day + timedelta(days=1), time.min, zone)
    return Axis(timezone=tz, start_ms=int(start.timestamp() * 1000),
                end_ms=int(end.timestamp() * 1000))


class SessionHeader(StrictModel):
    id: str
    src: Source
    title: str
    proj: str
    model: str | None
    start: int
    end: int
    d: date
    day_first_ms: int
    day_last_ms: int


class NeutralMarker(StrictModel):
    id: str
    session_id: str
    event_id: str | None
    t: int
    kind: Literal['rollback', 'compaction']
    evidence_status: Literal['observed', 'insufficient']
    action_state: Literal['attempted', 'succeeded', 'failed', 'unknown', 'not_applicable']


class DayRibbonResponse(StrictModel):
    contract_version: Literal[1] = 1
    date: date
    generated: datetime
    revision: str
    purge_revision: str
    day_basis: Literal['captured_local_day'] = 'captured_local_day'
    axis: Axis
    off_axis_event_ids: list[str]
    snapshot_complete: bool = True
    sessions: list[SessionHeader]
    events: list[DashboardEvent]
    markers_state: Literal['unsupported', 'partial', 'ready'] = 'unsupported'
    markers: list[NeutralMarker] = Field(default_factory=list)


class SessionSummary(StrictModel):
    summary: str | None
    summary_state: Literal['ready', 'pending', 'not_requested', 'failed']
    refresh_state: Literal['idle', 'pending', 'failed']
    generated_at: datetime | None
    model: str | None
    input_revision: str | None
    is_stale: bool


class ExtrasSections(StrictModel):
    summaries: Literal['ready', 'failed'] = 'ready'
    tokens: Literal['ready', 'failed'] = 'ready'
    findings: Literal['ready', 'failed', 'unsupported'] = 'unsupported'


class AnalysisState(StrictModel):
    state: Literal['not_requested', 'pending', 'ready', 'failed', 'unsupported']
    input_revision: str | None


class FindingEvidence(StrictModel):
    event_id: str
    role: Literal['request', 'claim', 'verification', 'contradiction']
    excerpt: str | None


class Finding(StrictModel):
    id: str
    revision: str
    session_id: str
    anchor_event_id: str | None
    t: int
    kind: Literal['verification_gap', 'user_reported_unresolved', 'possible_drift', 'possible_repetition']
    origin: Literal['mechanical', 'model']
    confidence: Literal['low', 'medium', 'high']
    state: Literal['open', 'dismissed', 'resolved']
    title: str
    explanation: str
    detector_version: str
    input_revision: str
    is_stale: bool
    evidence: list[FindingEvidence]


class DayExtrasResponse(StrictModel):
    contract_version: Literal[1] = 1
    date: date
    generated: datetime
    revision: str
    ribbon_revision: str
    purge_revision: str
    sections: ExtrasSections
    summaries_by_session: dict[str, SessionSummary]
    tokens: dict[str, DashboardTokens]
    tokens_by_source: dict[str, dict[Source, AgentTokens]]
    usage_scope: Literal['all_recorded_usage_on_captured_day'] = 'all_recorded_usage_on_captured_day'
    usage_only_session_count: int | None
    analysis_by_session: dict[str, AnalysisState]
    findings: list[Finding] = Field(default_factory=list)


class StoryRow(StrictModel):
    id: str
    t: int
    d: date
    s: str
    src: Source
    k: Literal['user', 'agent']
    x: str
    truncated: bool


class DayStoryResponse(StrictModel):
    contract_version: Literal[1] = 1
    date: date
    generated: datetime
    revision: str
    series_revision: str
    purge_revision: str
    has_newer_data: bool
    story: list[StoryRow]
    next_cursor: str | None


class InvalidStoryCursor(ValueError):
    pass


class ExpiredStoryCursor(InvalidStoryCursor):
    pass


class ChangedStoryCursor(InvalidStoryCursor):
    pass


class MissingDaySession(ValueError):
    pass
