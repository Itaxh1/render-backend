"""Write whole batches with a fixed number of database synchronization points.

executemany uses psycopg's pipeline mode. No per-record fetch is allowed until
that stage has finished, so network latency isn't multiplied by record count.
The caller owns the transaction and the per-device lock.
"""
from datetime import datetime, timedelta, timezone

from psycopg.types.json import Jsonb

from .ingest_sql import EVENT_UPSERT, RESULT_UPDATE, SESSION_UPSERT, SUMMARY_UPSERT, TOOL_UPSERT
from .session_identity import identity_keys
from .session_titles import task_title, TITLE_UPDATE


async def returning_rows(connection, statement, values):
    if not values:
        return []
    async with connection.cursor() as cursor:
        await cursor.executemany(statement, values, returning=True)
        rows = []
        for _ in values:
            rows.append(await cursor.fetchone())
            cursor.nextset()
        return rows


def record_key(record):
    return record.source_file_id, record.sequence, record.item_index


async def write_records(connection, principal, batch):
    # One ON CONFLICT target per batch; lower/repeated revisions are duplicates.
    unique = {}
    for record in batch.records:
        key = record_key(record)
        if key not in unique or unique[key].revision < record.revision:
            unique[key] = record
    keys = [{"f": f, "s": s, "i": i} for f, s, i in unique]
    previous = await (await connection.execute(
        """
        select e.source_file_id, e.source_sequence, e.source_item_index,
               e.revision, e.local_day, e.session_id, e.type
        from public.events e
        join jsonb_to_recordset(%s) as k(f text, s bigint, i integer)
          on e.source_file_id = k.f and e.source_sequence = k.s and e.source_item_index = k.i
        where e.user_id = %s and e.device_id = %s
        """, (Jsonb(keys), principal.user_id, principal.device_id),
    )).fetchall()
    old = {(r["source_file_id"], r["source_sequence"], r["source_item_index"]): r for r in previous}
    records = [r for key, r in sorted(unique.items())
               if key not in old or r.revision > old[key]["revision"]]
    if not records:
        return 0, len(batch.records)

    sessions = {}
    changed_days = set()
    for record in records:
        event = record.event
        key = (record.source, event.session_id)
        data = sessions.setdefault(key, {
            "title": None, "project": None, "model": None,
            "start": event.created_at, "end": event.created_at,
            "day": event.local_day, "revision": record.revision, "records": [],
        })
        data['records'].append(record)
        for column, value in (("title", event.session_title), ("project", event.project_name), ("model", event.model)):
            if value is not None:
                data[column] = value
        data["start"] = min(data["start"], event.created_at)
        data["end"] = max(data["end"], event.created_at)
        data["day"] = min(data["day"], event.local_day)
        data["revision"] = max(data["revision"], record.revision)
        changed_days.add(event.local_day)
        if record_key(record) in old:
            changed_days.add(old[record_key(record)]["local_day"])

    session_values = [
        (principal.user_id, principal.device_id, source, source_id,
         data["title"], data["project"], data["model"], data["start"], data["end"],
         data["day"], data["revision"], identity_keys(principal, source, source_id, data['records']))
        for (source, source_id), data in sessions.items()
    ]
    session_rows = await returning_rows(connection, SESSION_UPSERT, session_values)
    session_ids = {key: row["id"] for key, row in zip(sessions, session_rows)}
    records = [r for r in records if session_ids[(r.source,r.event.session_id)] is not None]
    if not records:
        return 0, len(batch.records)
    event_values = []
    for record in records:
        event = record.event
        event_values.append((
            principal.user_id, principal.device_id, session_ids[(record.source, event.session_id)],
            record.source_file_id, record.sequence, record.item_index, record.revision,
            bytes.fromhex(record.payload_hash), event.type, event.role, event.content_preview,
            event.model, event.source_call_id, event.token_input, event.token_output,
            event.token_cache_read, event.token_cache_write, event.token_thinking,
            event.usage_cumulative, event.created_at, event.local_day,
            "parsed" if record.stage == "enriched" else "skeleton", event.truncated, event.source_bytes,
        ))
    event_rows = await returning_rows(connection, EVENT_UPSERT, event_values)
    written = [(r, row["id"]) for r, row in zip(records, event_rows) if row is not None]
    if not written:
        return 0, len(batch.records)
    # Calls precede results even when the two arrive out of order in this batch.
    result_cursors = []
    async with connection.pipeline():
        for key, data in sessions.items():
            candidates = [(r.event.created_at, task_title(r.event.content_preview)) for r in data['records']
                          if r.event.type == 'user']
            candidates = sorted((time, title) for time, title in candidates if title)
            if candidates and session_ids[key] is not None:
                time, title = candidates[0]
                await connection.execute(TITLE_UPDATE, (title,time,principal.user_id,session_ids[key],time))
        for record, event_id in sorted(written, key=lambda pair: pair[0].event.type == "tool_result"):
            event = record.event
            session_id = session_ids[(record.source, event.session_id)]
            # Codex timestamps aren't a measured invocation duration. In
            # particular, a late result must not become hours of tool runtime.
            measured_duration = event.duration_ms if record.source == "claude-code" else None
            if event.type == "tool":
                await connection.execute(TOOL_UPSERT, (
                    principal.user_id, session_id, event_id,
                    event.source_call_id or f"{record.source_file_id}:{record.sequence}:{record.item_index}",
                    record.revision, event.tool_name or "unknown", event.tool_status,
                    event.tool_input_preview, event.tool_output_preview, event.exit_code,
                    event.created_at,
                    event.created_at + timedelta(milliseconds=measured_duration) if measured_duration is not None else None,
                    measured_duration, event.local_day,
                ))
            elif event.type == "tool_result" and event.source_call_id:
                result_cursors.append(await connection.execute(RESULT_UPDATE + " returning local_day", (
                    event.tool_status, event.tool_status, event.tool_output_preview,
                    event.exit_code, event.created_at, measured_duration,
                    record.source == "claude-code", event.created_at, event.created_at, record.revision,
                    principal.user_id, session_id, event.source_call_id,
                )))
            elif event.type != "tool_result":
                await connection.execute(
                    "delete from public.tool_calls where user_id = %s and event_id = %s",
                    (principal.user_id, event_id),
                )

    for cursor in result_cursors:
        for row in await cursor.fetchall():
            changed_days.add(row["local_day"])

    # Versions track corrections to existing IDs, not just append-only max(id).
    affected_sessions = sorted({session_ids[(r.source,r.event.session_id)] for r,_ in written} | {
        old[record_key(r)]['session_id'] for r in records
        if record_key(r) in old and 'session_id' in old[record_key(r)]
    })
    await connection.execute("""
        update public.sessions set summary_input_version=summary_input_version+1
        where user_id=%s and id=any(%s)
    """, (principal.user_id, affected_sessions))
    summary_inputs = await (await connection.execute(
        """
        select s.id session_id, s.summary_input_version input_revision,
               (max(e.created_at) at time zone 'UTC')::date active_day, max(e.created_at) last_event_at,
               array_agg(distinct e.local_day) days
        from public.sessions s join public.events e on e.user_id=s.user_id and e.session_id=s.id
        where s.user_id=%s and s.id=any(%s)
        group by s.id
        """, (principal.user_id, affected_sessions),
    )).fetchall()
    for data in summary_inputs:
        changed_days.update(data['days'])
    story_days, mutation_days = set(), set()
    for record in records:
        previous = old.get(record_key(record))
        if record.event.type in ('user','agent'):
            story_days.add(record.event.local_day)
        if previous and (previous.get('type') in ('user','agent') or record.event.type in ('user','agent')):
            mutation_days.update([previous['local_day'], record.event.local_day])
    story_days.update(mutation_days)
    today = datetime.now(timezone.utc).date()
    recent_cutoff = today - timedelta(days=6)
    async with connection.pipeline():
        await connection.execute(
            """
            insert into public.rollup_dirty(user_id, local_day, dirtied_at)
            select %s, day, now() from unnest(%s::date[]) as day
            on conflict (user_id, local_day) do update set dirtied_at = excluded.dirtied_at,
                generation=public.rollup_dirty.generation+1
            """, (principal.user_id, sorted(changed_days)),
        )
        for data in summary_inputs:
            if recent_cutoff <= data["active_day"] <= today:
                await connection.execute(SUMMARY_UPSERT, (
                    principal.user_id, data["session_id"], data["input_revision"], data["last_event_at"],
                ))
        await connection.execute(
            """
            insert into public.dashboard_changes(user_id, local_day, change_kind)
            select %s, day, 'events' from unnest(%s::date[]) as day
            """, (principal.user_id, sorted(changed_days)),
        )
        # Versions are locked last, after session/job writes, in day order.
        await connection.execute("select private.bump_day_versions(%s,%s::date[],true,true,false)",
                                 (principal.user_id, sorted(changed_days)))
        if story_days:
            await connection.execute("select private.bump_day_versions(%s,%s::date[],false,false,true)",
                                     (principal.user_id, sorted(story_days)))
        if mutation_days:
            await connection.execute("select private.bump_day_versions(%s,%s::date[],false,false,false,true)",
                                     (principal.user_id, sorted(mutation_days)))
    return len(written), len(batch.records) - len(written)
