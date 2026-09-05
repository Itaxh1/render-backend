"""Fixed SQL for pipelined batch ingestion; all values remain bound parameters."""

SESSION_UPSERT = """
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
"""

EVENT_UPSERT = """
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
"""

TOOL_UPSERT = """
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
"""

RESULT_UPDATE = """
update public.tool_calls
set status = case
      when %s = 'unknown' then status else %s
    end,
    output_preview = coalesce(%s, output_preview),
    exit_code = coalesce(%s, exit_code),
    ended_at = %s,
    duration_ms = coalesce(
      %s,
      case when %s and %s > started_at
        then extract(epoch from (%s - started_at)) * 1000
      end::bigint
    ),
    revision = greatest(revision, %s)
where user_id = %s and session_id = %s and source_call_id = %s
"""

SUMMARY_UPSERT = """
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
"""
