begin;
set local lock_timeout='5s';
alter table public.tool_calls alter constraint tool_calls_event_owner_fk deferrable initially immediate;
alter table public.tool_calls alter constraint tool_calls_session_owner_fk deferrable initially immediate;
commit;
