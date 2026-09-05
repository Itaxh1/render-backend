"""Write whole batches with a fixed number of database synchronization points.

executemany uses psycopg's pipeline mode. No per-record fetch is allowed until
that stage has finished, so network latency isn't multiplied by record count.
The caller owns the transaction and the per-device lock.
"""
from datetime import datetime, timedelta, timezone

from psycopg.types.json import Jsonb

from .ingest_sql import EVENT_UPSERT, RESULT_UPDATE, SESSION_UPSERT, SUMMARY_UPSERT, TOOL_UPSERT


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
               e.revision, e.local_day
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
            "day": event.local_day, "revision": record.revision,
        })
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
         data["day"], data["revision"])
        for (source, source_id), data in sessions.items()
    ]
    session_rows = await returning_rows(connection, SESSION_UPSERT, session_values)
    session_ids = {key: row["id"] for key, row in zip(sessions, session_rows)}
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
    # Calls precede results even when the two arrive out of order in this batch.
    result_cursors = []
    async with connection.pipeline():
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

    # One grouped read and one pipelined write phase, regardless of session count.
    summary_inputs = await (await connection.execute(
        """
        select session_id, max(id) as input_revision, max(local_day) as active_day,
               max(created_at) as last_event_at
        from public.events where user_id = %s and session_id = any(%s)
        group by session_id
        """, (principal.user_id, list(session_ids.values())),
    )).fetchall()
    recent_cutoff = datetime.now(timezone.utc).date() - timedelta(days=7)
    async with connection.pipeline():
        await connection.execute(
            """
            insert into public.rollup_dirty(user_id, local_day, dirtied_at)
            select %s, day, now() from unnest(%s::date[]) as day
            on conflict (user_id, local_day) do update set dirtied_at = excluded.dirtied_at
            """, (principal.user_id, sorted(changed_days)),
        )
        for data in summary_inputs:
            if data["active_day"] >= recent_cutoff:
                await connection.execute(SUMMARY_UPSERT, (
                    principal.user_id, data["session_id"], data["input_revision"], data["last_event_at"],
                ))
        await connection.execute(
            """
            insert into public.dashboard_changes(user_id, local_day, change_kind)
            select %s, day, 'events' from unnest(%s::date[]) as day
            """, (principal.user_id, sorted(changed_days)),
        )
    return len(written), len(batch.records) - len(written)
