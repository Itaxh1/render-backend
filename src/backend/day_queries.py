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
