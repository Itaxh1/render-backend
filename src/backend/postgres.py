from __future__ import annotations

import hashlib
import asyncio
import logging
from contextlib import suppress
import json
import secrets
from datetime import date, datetime, timedelta, timezone
from uuid import UUID

from psycopg.types.json import Jsonb
from .bulk_ingest import write_records
from .day_queries import RIBBON_EVENTS_SQL
from .day_reads import DayReads
from .day_versions import bump_session_extras, year_revisions
from .db_pool import pool
from . import rollups as rollup_jobs

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
    DashboardStory,
    DashboardTokens,
    DashboardTool,
    DevicePrincipal,
    IngestBatch,
    IngestReceipt,
)
from .store import BatchConflictError, InvalidClaimError, InvalidDeviceError, token_hash
from .usage import aggregate_usage
from .batch_identity import batch_payload


def _batch_hash(batch: IngestBatch) -> bytes:
    canonical = json.dumps(
        batch_payload(batch), sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(canonical).digest()


class PostgresStore(DayReads):
    def __init__(self, database_url: str) -> None:
        # One API process: 6 reads + 2 imports + 1 rollup; worker uses 1.
        self._pool = pool(database_url, maximum=6, waiting=32)
        self._ingest_pool = pool(database_url, maximum=2, waiting=8)
        self._rollup_pool = pool(database_url, maximum=1, waiting=2)

    async def open(self) -> None:
        await self._pool.open(wait=True)
        await self._ingest_pool.open(wait=True)
        await self._rollup_pool.open(wait=True)
        self._rollup_task = asyncio.create_task(self._rollup_loop())

    async def close(self) -> None:
        self._rollup_task.cancel()
        with suppress(asyncio.CancelledError):
            await self._rollup_task
        await self._pool.close()
        await self._ingest_pool.close()
        await self._rollup_pool.close()

    async def _rollup_loop(self) -> None:
        while True:
            try:
                worked = await self.refresh_rollups()
            except Exception as error:
                logging.getLogger(__name__).warning("rollup refresh failed: %s", type(error).__name__)
                worked = False
            await asyncio.sleep(0.1 if worked else 2)

    async def refresh_rollups(self) -> bool:
        async with self._rollup_pool.connection() as connection:
            async with connection.transaction():
                job = await rollup_jobs.claim(connection)
            if not job:
                return False
            async with connection.transaction():
                rows = await rollup_jobs.compute(connection, job)
            async with connection.transaction():
                await rollup_jobs.publish(connection, job, rows)
        return True

    async def calendar(self, user_id: UUID, year: int) -> CalendarPayload:
        async with self._pool.connection() as connection:
            data = await (await connection.execute(
                """
                select coalesce((select jsonb_agg(r) from public.daily_rollups r
                                 where r.user_id = %s and r.local_day >= %s and r.local_day < %s), '[]') as rollups,
                       exists(select 1 from public.rollup_dirty where user_id = %s) as pending,
                       coalesce((select max(sequence) from public.dashboard_changes where user_id = %s), 0) as revision,
                       exists(select 1 from public.devices where user_id = %s and revoked_at is null
                              and greatest(created_at, last_seen_at) > now() - interval '30 seconds') as active,
                       (select generation from private.day_api_state where singleton) as generation,
                       transaction_timestamp() as generated,
                       coalesce((select jsonb_agg(v) from public.day_versions v
                         where v.user_id=%s and v.local_day >= %s and v.local_day < %s),'[]') as versions
                """, (user_id, date(year, 1, 1), date(year + 1, 1, 1), user_id, user_id, user_id,
                      user_id, date(year, 1, 1), date(year + 1, 1, 1)),
            )).fetchone()
        rollups = {}
        for row in data["rollups"]:
            rollups.setdefault(row["local_day"], {})[row["source"]] = DashboardRollup(
                sessions=row["sessions"], events=row["events"], tools=row["tools"],
                ok=row["succeeded"], fail=row["failed"],
            )
        return CalendarPayload(
            generated=data['generated'], rollups=rollups, revision=data["revision"],
            day_revisions=year_revisions(data['generation'], user_id, year,
                [dict(row, local_day=date.fromisoformat(row['local_day'])) for row in data['versions']]),
            rollups_pending=data["pending"], refresh_after_ms=3000 if data["active"] or data["pending"] else 30000,
        )

    async def day_detail(self, user_id: UUID, day: date) -> DayPayload:
        data = await self.dashboard(user_id, day.year, day, detail_only=True)
        return DayPayload(**data.model_dump(include={"sessions", "events", "tools", "story", "tokens", "tokens_by_source"}))

    async def event_detail(self, user_id: UUID, event_id: str) -> EventDetail | None:
        if not event_id.isascii() or not event_id.isdecimal() or len(event_id) > 19 or int(event_id) > 9223372036854775807:
            return None
        async with self._pool.connection() as connection:
            row = await (await connection.execute(
                """
                select e.content_preview, e.truncated,
                       tc.input_preview, tc.output_preview
                from public.events e
                left join public.tool_calls tc on tc.event_id = e.id and tc.user_id = e.user_id
                where e.user_id = %s and e.id = %s and e.type in ('user', 'agent', 'tool')
                """, (user_id, int(event_id)),
            )).fetchone()
        if row is None:
            return None
        return EventDetail(id=event_id, content=row["content_preview"],
                           tool_input=row["input_preview"], tool_output=row["output_preview"],
                           truncated=row["truncated"])

    async def ready(self) -> bool:
        try:
            async with self._pool.connection(timeout=2.0) as connection:
                await connection.execute("select 1")
            return True
        except Exception:
            return False

    async def create_claim(self, user_id: UUID) -> tuple[str, datetime]:
        claim_token = secrets.token_urlsafe(32)
        expires_at = datetime.now(timezone.utc) + timedelta(minutes=10)
        async with self._pool.connection() as connection:
            async with connection.transaction():
                await connection.execute(
                    """
                    insert into private.device_claims(user_id, claim_hash, expires_at)
                    values (%s, %s, %s)
                    """,
                    (user_id, token_hash(claim_token), expires_at),
                )
        return claim_token, expires_at

    async def exchange_claim(
        self, claim_token: str, device_name: str, platform: str
    ) -> tuple[UUID, str]:
        digest = token_hash(claim_token)
        now = datetime.now(timezone.utc)
        device_token = secrets.token_urlsafe(32)
        async with self._pool.connection() as connection:
            async with connection.transaction():
                cursor = await connection.execute(
                    """
                    select id, user_id, expires_at, consumed_at
                    from private.device_claims
                    where claim_hash = %s
                    for update
                    """,
                    (digest,),
                )
                claim = await cursor.fetchone()
                if (
                    claim is None
                    or claim["consumed_at"] is not None
                    or claim["expires_at"] <= now
                ):
                    if claim is not None:
                        await connection.execute(
                            "update private.device_claims set attempts = attempts + 1 where id = %s",
                            (claim["id"],),
                        )
                    raise InvalidClaimError

                cursor = await connection.execute(
                    """
                    insert into public.devices(
                      user_id, name, platform, token_hash, token_expires_at
                    ) values (%s, %s, %s, %s, %s)
                    returning id
                    """,
                    (
                        claim["user_id"],
                        device_name,
                        platform,
                        token_hash(device_token),
                        now + timedelta(days=90),
                    ),
                )
                device = await cursor.fetchone()
                assert device is not None
                await connection.execute(
                    "update private.device_claims set consumed_at = %s where id = %s",
                    (now, claim["id"]),
                )
        return device["id"], device_token

    async def authenticate_device(self, device_token: str) -> DevicePrincipal | None:
        async with self._pool.connection() as connection:
            cursor = await connection.execute(
                """
                select id, user_id
                from public.devices
                where token_hash = %s
                  and revoked_at is null
                  and token_expires_at > now()
                """,
                (token_hash(device_token),),
            )
            row = await cursor.fetchone()
            if row is None:
                return None
            await connection.execute(
                "update public.devices set last_seen_at = now() where id = %s",
                (row["id"],),
            )
            return DevicePrincipal(device_id=row["id"], user_id=row["user_id"])

    async def list_devices(self, user_id: UUID) -> list[BrowserDevice]:
        async with self._pool.connection() as connection:
            rows = await (await connection.execute(
                """
                select d.id, d.name, d.platform, d.extractor_version,
                       d.created_at, d.last_seen_at,
                       case when d.revoked_at is not null then 'revoked'
                            when d.token_expires_at <= now() then 'expired'
                            else 'connected' end as status,
                       (select count(distinct k.session_id) from private.session_identity_keys k
                        where k.user_id=d.user_id and starts_with(k.identity_key,'device:'||d.id::text||':')) as sessions,
                       (select b.created_at from private.ingest_batches b
                        where b.user_id = d.user_id and b.device_id = d.id
                        order by b.device_sequence desc limit 1) as last_upload_at
                from public.devices d
                where d.user_id = %s
                order by d.created_at desc, d.id
                """,
                (user_id,),
            )).fetchall()
        return [BrowserDevice(**row) for row in rows]

    async def revoke_device(self, user_id: UUID, device_id: UUID) -> bool:
        async with self._pool.connection() as connection:
            cursor = await connection.execute(
                """
                update public.devices set revoked_at = coalesce(revoked_at, now())
                where user_id = %s and id = %s returning id
                """,
                (user_id, device_id),
            )
            return await cursor.fetchone() is not None

    async def ingest(
        self, principal: DevicePrincipal, batch: IngestBatch
    ) -> IngestReceipt:
        request_hash = _batch_hash(batch)
        async with self._ingest_pool.connection() as connection:
            async with connection.transaction():
                # SET LOCAL is also respected by transaction-pooling proxies.
                await connection.execute("set local statement_timeout='15s'; set local lock_timeout='2s'")
                cursor = await connection.execute(
                    """
                    select id from public.devices
                    where id = %s and user_id = %s
                      and revoked_at is null and token_expires_at > now()
                    for update
                    """,
                    (principal.device_id, principal.user_id),
                )
                if await cursor.fetchone() is None:
                    raise InvalidDeviceError
                cursor = await connection.execute(
                    """
                    select batch_id, request_hash, accepted, duplicate, rejected
                    from private.ingest_batches
                    where device_id = %s and (batch_id = %s or device_sequence = %s)
                    for update
                    """,
                    (principal.device_id, batch.batch_id, batch.device_sequence),
                )
                previous = await cursor.fetchone()
                if previous is not None:
                    if (
                        previous["batch_id"] != batch.batch_id
                        or bytes(previous["request_hash"]) != request_hash
                    ):
                        raise BatchConflictError
                    return IngestReceipt(batch_id=batch.batch_id, **{
                        key: previous[key] for key in ("accepted", "duplicate", "rejected")
                    })

                accepted, duplicate = await write_records(connection, principal, batch)

                receipt = IngestReceipt(
                    batch_id=batch.batch_id,
                    accepted=accepted,
                    duplicate=duplicate,
                    rejected=0,
                )
                await connection.execute(
                    """
                    insert into private.ingest_batches(
                      user_id, device_id, batch_id, device_sequence, request_hash,
                      accepted, duplicate, rejected, receipt
                    ) values (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        principal.user_id,
                        principal.device_id,
                        batch.batch_id,
                        batch.device_sequence,
                        request_hash,
                        accepted,
                        duplicate,
                        0,
                        Jsonb(receipt.model_dump(mode="json")),
                    ),
                )
                await connection.execute(
                    """
                    update public.devices
                    set extractor_version = greatest(extractor_version, %s)
                    where id = %s and user_id = %s
                    """,
                    (batch.extractor_version, principal.device_id, principal.user_id),
                )
                return receipt

    async def dashboard(
        self, user_id: UUID, year: int, day: date | None = None, *, detail_only: bool = False
    ) -> DashboardPayload:
        start = day if detail_only else date(year, 1, 1)
        end = day + timedelta(days=1) if detail_only else date(year + 1, 1, 1)
        async with self._pool.connection() as connection:
            if detail_only:
                rollup_rows = []
            else:
                rollup_rows = await (
                    await connection.execute(
                        """
                        select e.local_day, s.source,
                               count(distinct e.session_id)::integer as sessions,
                               count(*) filter (where e.type in ('user', 'agent', 'tool'))::integer as events,
                               count(tc.id)::integer as tools,
                               count(tc.id) filter (where tc.status = 'succeeded')::integer as succeeded,
                               count(tc.id) filter (where tc.status = 'failed')::integer as failed
                        from public.events e
                        join public.sessions s
                          on s.user_id = e.user_id and s.id = e.session_id
                        left join public.tool_calls tc
                          on tc.user_id = e.user_id and tc.event_id = e.id
                        where e.user_id = %s and e.local_day >= %s and e.local_day < %s
                        group by e.local_day, s.source
                        order by e.local_day, s.source
                        """,
                        (user_id, start, end),
                    )
                ).fetchall()
            selected_day = day or (
                rollup_rows[-1]["local_day"] if rollup_rows else start
            )
            session_rows = await (
                await connection.execute(
                    """
                    with active as (
                      select distinct session_id
                      from public.events
                      where user_id = %s and local_day = %s
                    )
                    select s.id, s.source,
                           coalesce(
                             nullif(s.display_title, ''), nullif(s.title, ''),
                             nullif(s.project_name, '') || ' session', 'Untitled session'
                           ) as title,
                           coalesce(nullif(s.project_name, ''), 'Unknown project') as project_name,
                           s.model, s.started_at, coalesce(s.ended_at, s.last_event_at) as ended_at,
                           %s::date as display_day, summary.tldr,
                           case
                             when summary.tldr is not null then 'ready'
                             when job.status in ('pending', 'processing') then 'pending'
                             when job.status = 'failed' then 'failed'
                             else 'not_requested'
                           end as summary_state
                    from active a
                    join public.sessions s on s.user_id = %s and s.id = a.session_id
                    left join lateral (
                      select tldr
                      from public.summaries sm
                      where sm.user_id = s.user_id and sm.session_id = s.id
                      order by sm.input_revision desc
                      limit 1
                    ) summary on true
                    left join private.summary_jobs job
                      on job.user_id = s.user_id and job.session_id = s.id
                    order by s.started_at, s.id
                    """,
                    (user_id, selected_day, selected_day, user_id),
                )
            ).fetchall()
            event_rows = await (
                await connection.execute(
                    RIBBON_EVENTS_SQL,
                    (user_id, selected_day, user_id),
                )
            ).fetchall()
            story_rows = await (
                await connection.execute(
                    """
                    select e.created_at, e.local_day, s.source, e.session_id,
                           e.type, e.content_preview
                    from public.events e
                    join public.sessions s
                      on s.user_id = e.user_id and s.id = e.session_id
                    where e.user_id = %s and e.local_day = %s
                      and e.type in ('user', 'agent') and e.content_preview is not null
                    order by e.created_at, e.id
                    """,
                    (user_id, selected_day),
                )
            ).fetchall()
            # Limit the session set, not its history: LAG needs earlier samples
            # across midnight/year boundaries. Legacy dashboard uses a year range.
            # ANY(array(...)) gives the usage index a bounded session lookup;
            # IN(subquery) can instead merge-scan unrelated historical sessions.
            token_rows = await (
                await connection.execute(
                    """
                    with active as (
                      select distinct session_id
                      from public.events
                      where user_id = %s and local_day >= %s and local_day < %s
                    ), samples as (
                      select e.local_day, s.source, e.session_id, e.created_at, e.id,
                             e.usage_cumulative,
                             coalesce(e.token_input, 0) as token_input,
                             coalesce(e.token_output, 0) as token_output,
                             coalesce(e.token_cache_read, 0) as token_cache_read,
                             coalesce(e.token_cache_write, 0) as token_cache_write,
                             coalesce(e.token_thinking, 0) as token_thinking,
                             lag(coalesce(e.token_input, 0)) over w as previous_input,
                             lag(coalesce(e.token_output, 0)) over w as previous_output,
                             lag(coalesce(e.token_cache_read, 0)) over w as previous_cache_read,
                             lag(coalesce(e.token_cache_write, 0)) over w as previous_cache_write,
                             lag(coalesce(e.token_thinking, 0)) over w as previous_thinking
                      from public.events e
                      join public.sessions s on s.id = e.session_id and s.user_id = e.user_id
                      where e.user_id = %s
                        and e.session_id = any(array(select session_id from active))
                        and (e.token_input is not null or e.token_output is not null)
                      window w as (partition by e.session_id order by e.created_at, e.id)
                    ), deltas as (
                      select local_day, source,
                             case when usage_cumulative then
                               case when previous_input is null or token_input < previous_input
                                 then token_input else token_input - previous_input end
                               else token_input end as token_input,
                             case when usage_cumulative then
                               case when previous_output is null or token_output < previous_output
                                 then token_output else token_output - previous_output end
                               else token_output end as token_output,
                             case when usage_cumulative then
                               case when previous_cache_read is null or token_cache_read < previous_cache_read
                                 then token_cache_read else token_cache_read - previous_cache_read end
                               else token_cache_read end as token_cache_read,
                             case when usage_cumulative then
                               case when previous_cache_write is null or token_cache_write < previous_cache_write
                                 then token_cache_write else token_cache_write - previous_cache_write end
                               else token_cache_write end as token_cache_write,
                             case when usage_cumulative then
                               case when previous_thinking is null or token_thinking < previous_thinking
                                 then token_thinking else token_thinking - previous_thinking end
                               else token_thinking end as token_thinking
                      from samples
                    )
                    select local_day, source, sum(token_input)::bigint as token_input,
                           sum(token_output)::bigint as token_output,
                           sum(token_cache_read)::bigint as token_cache_read,
                           sum(token_cache_write)::bigint as token_cache_write,
                           sum(token_thinking)::bigint as token_thinking
                    from deltas
                    where local_day >= %s and local_day < %s
                    group by local_day, source order by local_day, source
                    """,
                    (user_id, start, end, user_id, start, end),
                )
            ).fetchall()
            tool_rows = await (
                await connection.execute(
                    """
                    select tool_name, count(*)::integer as count,
                           count(*) filter (where status = 'succeeded')::integer as succeeded,
                           count(*) filter (where status = 'failed')::integer as failed,
                           coalesce(percentile_cont(0.5) within group (order by duration_ms)
                             filter (where duration_ms is not null), 0)::bigint as p50,
                           coalesce(percentile_cont(0.9) within group (order by duration_ms)
                             filter (where duration_ms is not null), 0)::bigint as p90,
                           coalesce(max(duration_ms), 0)::bigint as maximum
                    from public.tool_calls
                    where user_id = %s and local_day = %s
                    group by tool_name order by count(*) desc, tool_name
                    """,
                    (user_id, selected_day),
                )
            ).fetchall()
            if detail_only:
                stat_row = {"files": 0, "source_bytes": 0, "strokes": 0}
            else:
                stat_row = await (
                    await connection.execute(
                        """
                        with selected as (
                          select * from public.events
                          where user_id = %s and local_day >= %s and local_day < %s
                        ), source_records as (
                          select source_file_id, source_sequence, max(source_bytes) as source_bytes
                          from selected group by source_file_id, source_sequence
                        )
                        select (select count(distinct source_file_id) from selected)::integer as files,
                               coalesce((select sum(source_bytes) from source_records), 0)::bigint as source_bytes,
                               (select count(*) from selected
                                 where type in ('user', 'agent', 'tool'))::integer as strokes
                        """,
                        (user_id, start, end),
                    )
                ).fetchone()


        rollups: dict[str, dict] = {}
        for row in rollup_rows:
            rollups.setdefault(row["local_day"].isoformat(), {})[row["source"]] = DashboardRollup(
                sessions=row["sessions"], events=row["events"], tools=row["tools"],
                ok=row["succeeded"], fail=row["failed"],
            )
        events = [DashboardEvent(
            id=str(row["id"]), t=int(row["created_at"].timestamp() * 1000),
            d=row["local_day"], src=row["source"], s=str(row["session_id"]),
            k=row["type"],
            st=(row["status"] if row["type"] == "tool" else "succeeded") or "unknown",
            n=row["tool_name"], ms=row["duration_ms"] if row['source'] == 'claude-code' else None,
        ) for row in event_rows]
        tokens, tokens_by_source = aggregate_usage(token_rows)
        return DashboardPayload(
            generated=datetime.now(timezone.utc),
            rollups=rollups,
            sessions=[DashboardSession(
                id=str(row["id"]), src=row["source"], title=row["title"],
                proj=row["project_name"], model=row["model"],
                start=int(row["started_at"].timestamp() * 1000),
                end=int(row["ended_at"].timestamp() * 1000), d=row["display_day"],
                summary=row["tldr"], summary_state=row["summary_state"],
            ) for row in session_rows],
            events=events,
            tools=[DashboardTool(
                name=row["tool_name"], count=row["count"], ok=row["succeeded"],
                fail=row["failed"], p50=row["p50"], p90=row["p90"], max=row["maximum"],
            ) for row in tool_rows],
            tokens=tokens,
            tokens_by_source=tokens_by_source,
            story=[DashboardStory(
                t=int(row["created_at"].timestamp() * 1000), d=row["local_day"],
                src=row["source"], s=str(row["session_id"]), k=row["type"],
                x=row["content_preview"],
            ) for row in story_rows],
            stats=DashboardStats(
                files=stat_row["files"],
                corpus_gb=round(stat_row["source_bytes"] / 1_000_000_000, 2),
                strokes=stat_row["strokes"],
            ),
        )

    async def request_summary(self, user_id: UUID, session_id: int) -> bool:
        async with self._pool.connection() as connection:
            async with connection.transaction():
                row = await (
                    await connection.execute(
                        """
                        select s.summary_input_version as input_revision
                        from public.sessions s where s.user_id=%s and s.id=%s
                          and exists(select 1 from public.events e where e.user_id=s.user_id and e.session_id=s.id)
                        for update
                        """,
                        (user_id, session_id),
                    )
                ).fetchone()
                if row is None or row["input_revision"] is None:
                    return False
                saved = await (await connection.execute(
                    """select 1 from public.summaries
                       where user_id = %s and session_id = %s and input_revision = %s""",
                    (user_id, session_id, row["input_revision"]),
                )).fetchone()
                if saved:
                    return True
                changed = await (await connection.execute(
                    """
                    insert into private.summary_jobs(
                      user_id, session_id, input_revision, status, available_at, updated_at,requested_explicitly
                    ) values (%s, %s, %s, 'pending', now(), now(),true)
                    on conflict (user_id, session_id) do update set
                      input_revision = excluded.input_revision,
                      requested_explicitly=true,
                      status = case when private.summary_jobs.status='processing' then 'processing' else 'pending' end,
                      available_at = now(),
                      locked_at = case when private.summary_jobs.status='processing' then private.summary_jobs.locked_at else null end,
                      lease_token = case when private.summary_jobs.status='processing' then private.summary_jobs.lease_token else null end,
                      attempts = case when private.summary_jobs.status='processing' then private.summary_jobs.attempts else 0 end,
                      last_error = null, updated_at = now()
                    where private.summary_jobs.input_revision <> excluded.input_revision
                       or private.summary_jobs.status in ('failed','completed')
                       or not private.summary_jobs.requested_explicitly
                    returning id
                    """,
                    (user_id, session_id, row["input_revision"]),
                )).fetchone()
                if changed:
                    await bump_session_extras(connection, user_id, session_id)
                return True
