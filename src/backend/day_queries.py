"""Day-bounded reads shared by the legacy dashboard and split day API."""

RIBBON_EVENTS_SQL = """
with day_events as materialized (
  select id, user_id, created_at, local_day, session_id, type
  from public.events
  where user_id = %s and local_day = %s
    and type in ('user', 'agent', 'tool')
), day_tools as materialized (
  select tc.event_id, tc.user_id, tc.status, tc.tool_name, tc.duration_ms
  from public.tool_calls tc
  where tc.user_id = %s
    and tc.session_id = any(array(select distinct session_id from day_events))
)
select e.id, e.created_at, e.local_day, s.source,
       e.session_id, e.type, tc.status, tc.tool_name, tc.duration_ms
from day_events e
join public.sessions s on s.user_id = e.user_id and s.id = e.session_id
left join day_tools tc on tc.user_id = e.user_id and tc.event_id = e.id
order by e.created_at, e.id
"""

RIBBON_SESSIONS_SQL = """
with active as (
  select session_id, min(created_at) day_first, max(created_at) day_last
  from public.events where user_id=%s and local_day=%s
    and type in ('user','agent','tool') group by session_id
)
select s.id,s.source,s.model,s.started_at,coalesce(s.ended_at,s.last_event_at) ended_at,
       coalesce(nullif(s.display_title,''),nullif(s.title,''),nullif(s.project_name,'')||' session','Untitled session') title,
       coalesce(nullif(s.project_name,''),'Unknown project') project_name,
       a.day_first,a.day_last
from active a join public.sessions s on s.user_id=%s and s.id=a.session_id
order by a.day_first,s.id
"""

SUMMARY_ROWS_SQL = """
with active as (
  select session_id, bool_or(type in ('user','agent','tool')) visible
  from public.events where user_id=%s and local_day=%s group by session_id
)
select s.id,s.summary_input_version,a.visible,
       sm.tldr,sm.input_revision,sm.generated_at,sm.model,j.status job_status
from active a join public.sessions s on s.user_id=%s and s.id=a.session_id
left join lateral (
  select tldr,input_revision,generated_at,model from public.summaries sm
  where sm.user_id=s.user_id and sm.session_id=s.id
  order by input_revision desc limit 1
) sm on true
left join private.summary_jobs j on j.user_id=s.user_id and j.session_id=s.id
order by s.id
"""

# Every field is a compile-time identifier. Filtering sessions rather than days
# inside the windows retains the preceding cumulative sample across midnight.
_USAGE_FIELDS = ('token_input','token_output','token_cache_read','token_cache_write','token_thinking')
TOKEN_ROWS_SQL = """
with active as (
  select distinct session_id from public.events
  where user_id=%s and local_day >= %s and local_day < %s
), samples as (
  select e.local_day,s.source,e.usage_cumulative,
""" + ',\n'.join(
    f'coalesce(e.{f},0) as {f}, lag(coalesce(e.{f},0)) over w as previous_{f}'
    for f in _USAGE_FIELDS
) + """
  from public.events e join public.sessions s on s.user_id=e.user_id and s.id=e.session_id
  where e.user_id=%s and e.session_id=any(array(select session_id from active))
    and (e.token_input is not null or e.token_output is not null)
  window w as (partition by e.session_id order by e.created_at,e.id)
), deltas as (
  select local_day,source,
""" + ',\n'.join(
    f'case when usage_cumulative and previous_{f} is not null and {f} >= previous_{f} '
    f'then {f}-previous_{f} else {f} end as {f}' for f in _USAGE_FIELDS
) + '\n from samples) select local_day,source,' + ','.join(
    f'sum({f})::bigint as {f}' for f in _USAGE_FIELDS
) + '\n from deltas where local_day >= %s and local_day < %s group by local_day,source order by local_day,source'
