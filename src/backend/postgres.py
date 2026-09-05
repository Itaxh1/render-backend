from __future__ import annotations

import hashlib
import json
import secrets
from datetime import date, datetime, timedelta, timezone
from uuid import UUID

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from .models import (
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
from .store import BatchConflictError, InvalidClaimError, token_hash


def _batch_hash(batch: IngestBatch) -> bytes:
    canonical = json.dumps(
        batch.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(canonical).digest()


class PostgresStore:
    def __init__(self, database_url: str) -> None:
        self._pool = AsyncConnectionPool(
            database_url,
            min_size=1,
            max_size=10,
            open=False,
            kwargs={"row_factory": dict_row},
        )

    async def open(self) -> None:
        await self._pool.open(wait=True)

    async def close(self) -> None:
        await self._pool.close()

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

    async def ingest(
        self, principal: DevicePrincipal, batch: IngestBatch
    ) -> IngestReceipt:
        request_hash = _batch_hash(batch)
        async with self._pool.connection() as connection:
            async with connection.transaction():
                await connection.execute(
                    "select id from public.devices where id = %s for update",
                    (principal.device_id,),
                )
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

                accepted = 0
                duplicate = 0
                changed_days = set()
                changed_sessions = set()
                ordered_records = sorted(
                    batch.records, key=lambda item: (item.source_file_id, item.sequence)
                )
                for record in ordered_records:
                    event = record.event
                    cursor = await connection.execute(
                        """
                        insert into public.sessions(
                          user_id, device_id, source, source_session_id,
                          title, project_name, model, started_at, last_event_at,
                          started_day, revision
                        ) values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        on conflict (user_id, device_id, source, source_session_id)
                        do update set
                          started_at = least(public.sessions.started_at, excluded.started_at),
                          started_day = least(public.sessions.started_day, excluded.started_day),
                          last_event_at = greatest(public.sessions.last_event_at, excluded.last_event_at),
                          title = coalesce(excluded.title, public.sessions.title),
                          project_name = coalesce(excluded.project_name, public.sessions.project_name),
                          model = coalesce(excluded.model, public.sessions.model),
                          revision = greatest(public.sessions.revision, excluded.revision)
                        returning id
                        """,
                        (
                            principal.user_id,
                            principal.device_id,
                            record.source,
                            event.session_id,
                            event.session_title,
                            event.project_name,
                            event.model,
                            event.created_at,
                            event.created_at,
                            event.local_day,
                            record.revision,
                        ),
                    )
                    session = await cursor.fetchone()
                    assert session is not None

                    cursor = await connection.execute(
                        """
                        select revision, local_day
                        from public.events
                        where device_id = %s
                          and source_file_id = %s
                          and source_sequence = %s
                          and source_item_index = %s
                        for update
                        """,
                        (
                            principal.device_id, record.source_file_id,
                            record.sequence, record.item_index,
                        ),
                    )
                    previous_event = await cursor.fetchone()
                    if previous_event is not None and previous_event["revision"] >= record.revision:
                        duplicate += 1
                        continue
                    if previous_event is not None:
                        changed_days.add(previous_event["local_day"])

                    cursor = await connection.execute(
                        """
                        insert into public.events(
                          user_id, device_id, session_id, source_file_id,
                          source_sequence, source_item_index, revision, payload_hash,
                          type, role, content_preview, model, source_call_id,
                          token_input, token_output, token_cache_read,
                          token_cache_write, token_thinking, usage_cumulative,
                          created_at, local_day, parse_status, truncated, source_bytes
                        ) values (
                          %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                          %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                          %s, %s, %s, %s
                        )
                        on conflict (device_id, source_file_id, source_sequence, source_item_index)
                        do update set
                          session_id = excluded.session_id,
                          revision = excluded.revision,
                          payload_hash = excluded.payload_hash,
                          type = excluded.type,
                          role = excluded.role,
                          content_preview = excluded.content_preview,
                          model = excluded.model,
                          source_call_id = excluded.source_call_id,
                          token_input = excluded.token_input,
                          token_output = excluded.token_output,
                          token_cache_read = excluded.token_cache_read,
                          token_cache_write = excluded.token_cache_write,
                          token_thinking = excluded.token_thinking,
                          usage_cumulative = excluded.usage_cumulative,
                          created_at = excluded.created_at,
                          local_day = excluded.local_day,
                          parse_status = excluded.parse_status,
                          truncated = excluded.truncated,
                          source_bytes = excluded.source_bytes
                        where public.events.revision < excluded.revision
                        returning id
                        """,
                        (
                            principal.user_id,
                            principal.device_id,
                            session["id"],
                            record.source_file_id,
                            record.sequence,
                            record.item_index,
                            record.revision,
                            bytes.fromhex(record.payload_hash),
                            event.type,
                            event.role,
                            event.content_preview,
                            event.model,
                            event.source_call_id,
                            event.token_input,
                            event.token_output,
                            event.token_cache_read,
                            event.token_cache_write,
                            event.token_thinking,
                            event.usage_cumulative,
                            event.created_at,
                            event.local_day,
                            "parsed" if record.stage == "enriched" else "skeleton",
                            event.truncated,
                            event.source_bytes,
                        ),
                    )
                    stored_event = await cursor.fetchone()
                    if stored_event is None:
                        duplicate += 1
                        continue
                    accepted += 1
                    changed_days.add(event.local_day)
                    changed_sessions.add(session["id"])

                    if event.type == "tool":
                        source_call_id = event.source_call_id or (
                            f"{record.source_file_id}:{record.sequence}:{record.item_index}"
                        )
                        await connection.execute(
                            """
                            insert into public.tool_calls(
                              user_id, session_id, event_id, source_call_id, revision,
                              tool_name, status, input_preview, output_preview,
                              exit_code, started_at, ended_at, duration_ms, local_day
                            ) values (
                              %s, %s, %s, %s, %s, %s, %s, %s, %s,
                              %s, %s, %s, %s, %s
                            )
                            on conflict (user_id, session_id, source_call_id)
                            do update set
                              event_id = excluded.event_id,
                              revision = excluded.revision,
                              tool_name = excluded.tool_name,
                              status = excluded.status,
                              input_preview = excluded.input_preview,
                              output_preview = excluded.output_preview,
                              exit_code = excluded.exit_code,
                              started_at = excluded.started_at,
                              ended_at = excluded.ended_at,
                              duration_ms = excluded.duration_ms,
                              local_day = excluded.local_day
                            where public.tool_calls.revision < excluded.revision
                            """,
                            (
                                principal.user_id,
                                session["id"],
                                stored_event["id"],
                                source_call_id,
                                record.revision,
                                event.tool_name or "unknown",
                                event.tool_status,
                                event.tool_input_preview,
                                event.tool_output_preview,
                                event.exit_code,
                                event.created_at,
                                (
                                    event.created_at + timedelta(milliseconds=event.duration_ms)
                                    if event.duration_ms is not None else None
                                ),
                                event.duration_ms,
                                event.local_day,
                            ),
                        )
                    elif event.type == "tool_result" and event.source_call_id:
                        await connection.execute(
                            """
                            update public.tool_calls
                            set status = case
                                  when %s = 'unknown' then status else %s
                                end,
                                output_preview = coalesce(%s, output_preview),
                                exit_code = coalesce(%s, exit_code),
                                ended_at = %s,
                                duration_ms = coalesce(
                                  %s,
                                  case when %s > started_at
                                    then extract(epoch from (%s - started_at)) * 1000
                                  end::bigint
                                ),
                                revision = greatest(revision, %s)
                            where user_id = %s and session_id = %s and source_call_id = %s
                            """,
                            (
                                event.tool_status, event.tool_status,
                                event.tool_output_preview, event.exit_code,
                                event.created_at, event.duration_ms,
                                event.created_at, event.created_at,
                                record.revision, principal.user_id, session["id"],
                                event.source_call_id,
                            ),
                        )
                    elif event.type != "tool_result":
                        await connection.execute(
                            "delete from public.tool_calls where user_id = %s and event_id = %s",
                            (principal.user_id, stored_event["id"]),
                        )

                for day in sorted(changed_days):
                    await connection.execute(
                        """
                        insert into public.rollup_dirty(user_id, local_day, dirtied_at)
                        values (%s, %s, now())
                        on conflict (user_id, local_day)
                        do update set dirtied_at = excluded.dirtied_at
                        """,
                        (principal.user_id, day),
                    )

                recent_cutoff = datetime.now(timezone.utc).date() - timedelta(days=7)
                for session_id in sorted(changed_sessions):
                    cursor = await connection.execute(
                        """
                        select max(id) as input_revision, max(local_day) as active_day,
                               max(created_at) as last_event_at
                        from public.events
                        where user_id = %s and session_id = %s
                        """,
                        (principal.user_id, session_id),
                    )
                    summary_input = await cursor.fetchone()
                    if (
                        summary_input is None
                        or summary_input["input_revision"] is None
                        or summary_input["active_day"] < recent_cutoff
                    ):
                        continue
                    await connection.execute(
                        """
                        insert into private.summary_jobs(
                          user_id, session_id, input_revision, status, available_at, updated_at
                        ) values (%s, %s, %s, 'pending', greatest(now(), %s + interval '60 seconds'), now())
                        on conflict (user_id, session_id) do update set
                          input_revision = excluded.input_revision,
                          status = 'pending',
                          available_at = excluded.available_at,
                          locked_at = null,
                          last_error = null,
                          updated_at = now()
                        where private.summary_jobs.input_revision < excluded.input_revision
                        """,
                        (
                            principal.user_id, session_id,
                            summary_input["input_revision"], summary_input["last_event_at"],
                        ),
                    )
                    await connection.execute(
                        """
                        insert into public.dashboard_changes(user_id, local_day, change_kind)
                        values (%s, %s, 'events')
                        """,
                        (principal.user_id, day),
                    )

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
                return receipt

    async def dashboard(
        self, user_id: UUID, year: int, day: date | None = None
    ) -> DashboardPayload:
        start = date(year, 1, 1)
        end = date(year + 1, 1, 1)
        async with self._pool.connection() as connection:
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
                    select s.id, s.source,
                           coalesce(
                             nullif(s.title, ''), nullif(left(first_prompt.content_preview, 120), ''),
                             nullif(s.project_name, ''), 'Untitled session'
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
                    from public.sessions s
                    left join lateral (
                      select tldr
                      from public.summaries sm
                      where sm.user_id = s.user_id and sm.session_id = s.id
                      order by sm.input_revision desc
                      limit 1
                    ) summary on true
                    left join lateral (
                      select content_preview
                      from public.events first_event
                      where first_event.user_id = s.user_id
                        and first_event.session_id = s.id and first_event.type = 'user'
                        and first_event.content_preview is not null
                      order by first_event.created_at, first_event.id
                      limit 1
                    ) first_prompt on true
                    left join private.summary_jobs job
                      on job.user_id = s.user_id and job.session_id = s.id
                    where s.user_id = %s and exists (
                      select 1 from public.events selected_event
                      where selected_event.user_id = s.user_id
                        and selected_event.session_id = s.id
                        and selected_event.local_day = %s
                    )
                    order by s.started_at, s.id
                    """,
                    (selected_day, user_id, selected_day),
                )
            ).fetchall()
            event_rows = await (
                await connection.execute(
                    """
                    select e.id, e.created_at, e.local_day, s.source,
                           e.session_id, e.type, tc.status, tc.tool_name, tc.duration_ms
                    from public.events e
                    join public.sessions s
                      on s.user_id = e.user_id and s.id = e.session_id
                    left join public.tool_calls tc
                      on tc.user_id = e.user_id and tc.event_id = e.id
                    where e.user_id = %s and e.local_day = %s
                      and e.type in ('user', 'agent', 'tool')
                    order by e.created_at, e.id
                    """,
                    (user_id, selected_day),
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
            token_rows = await (
                await connection.execute(
                    """
                    with samples as (
                      select e.local_day, e.session_id, e.created_at, e.id,
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
                      where e.user_id = %s
                        and (e.token_input is not null or e.token_output is not null)
                      window w as (partition by e.session_id order by e.created_at, e.id)
                    ), deltas as (
                      select local_day,
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
                    select local_day, sum(token_input)::bigint as token_input,
                           sum(token_output)::bigint as token_output,
                           sum(token_cache_read)::bigint as token_cache_read,
                           sum(token_cache_write)::bigint as token_cache_write,
                           sum(token_thinking)::bigint as token_thinking
                    from deltas
                    where local_day >= %s and local_day < %s
                    group by local_day order by local_day
                    """,
                    (user_id, start, end),
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
            n=row["tool_name"], ms=row["duration_ms"],
        ) for row in event_rows]
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
            tokens={row["local_day"].isoformat(): DashboardTokens(
                input=row["token_input"], out=row["token_output"],
                cr=row["token_cache_read"], cw=row["token_cache_write"],
                th=row["token_thinking"],
            ) for row in token_rows},
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
                        select max(e.id) as input_revision
                        from public.events e
                        where e.user_id = %s and e.session_id = %s
                        """,
                        (user_id, session_id),
                    )
                ).fetchone()
                if row is None or row["input_revision"] is None:
                    return False
                await connection.execute(
                    """
                    insert into private.summary_jobs(
                      user_id, session_id, input_revision, status, available_at, updated_at
                    ) values (%s, %s, %s, 'pending', now(), now())
                    on conflict (user_id, session_id) do update set
                      input_revision = excluded.input_revision,
                      status = 'pending', available_at = now(), locked_at = null,
                      last_error = null, updated_at = now()
                    """,
                    (user_id, session_id, row["input_revision"]),
                )
                return True
