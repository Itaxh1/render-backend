begin;
set local lock_timeout='5s';
set local statement_timeout='120s';

-- Private material never enters calendar/day responses. Rotate generation after
-- a database restore; rotate cursor_key to invalidate outstanding cursors.
create table private.day_api_state (
  singleton boolean primary key default true check (singleton),
  generation uuid not null default gen_random_uuid(),
  cursor_key text not null default (gen_random_uuid()::text || gen_random_uuid()::text)
);
insert into private.day_api_state(singleton) values (true);
revoke all on private.day_api_state from public, anon, authenticated;
alter table private.day_api_state enable row level security;

create table public.day_versions (
  user_id uuid not null references auth.users(id) on delete cascade,
  local_day date not null,
  ribbon bigint not null default 0,
  extras bigint not null default 0,
  story bigint not null default 0,
  story_mutation bigint not null default 0,
  purge bigint not null default 0,
  updated_at timestamptz not null default now(),
  primary key (user_id, local_day)
);
alter table public.day_versions enable row level security;
create policy day_versions_read_own on public.day_versions for select to authenticated
  using ((select auth.uid()) = user_id);
revoke all on public.day_versions from public, anon, authenticated;
grant select on public.day_versions to authenticated;

insert into public.day_versions(user_id, local_day, ribbon, extras, story)
select user_id, local_day, 1, 1, 1 from public.events group by user_id, local_day;

-- Invoker-only: ordinary browser roles cannot write the table or call this.
create function private.bump_day_versions(
  owner_id uuid, days date[], r boolean, e boolean, s boolean,
  m boolean default false, p boolean default false
) returns void language sql set search_path = pg_catalog as $$
  insert into public.day_versions(user_id, local_day, ribbon, extras, story, story_mutation, purge)
  select owner_id, d, r::int, e::int, s::int, m::int, p::int
  from (select distinct unnest(days) as d) dates
  where exists (select 1 from auth.users where id = owner_id)
  order by d
  on conflict(user_id, local_day) do update set
    ribbon = public.day_versions.ribbon + excluded.ribbon,
    extras = public.day_versions.extras + excluded.extras,
    story = public.day_versions.story + excluded.story,
    story_mutation = public.day_versions.story_mutation + excluded.story_mutation,
    purge = public.day_versions.purge + excluded.purge,
    updated_at = now();
$$;
revoke all on function private.bump_day_versions(uuid,date[],boolean,boolean,boolean,boolean,boolean)
  from public, anon, authenticated;

alter table public.sessions add column summary_input_version bigint not null default 1;
-- One grouped scan per source, not a backwards primary-key probe per session.
with event_versions as materialized (
  select user_id,session_id,max(id) revision from public.events group by user_id,session_id
), summary_versions as materialized (
  select user_id,session_id,max(input_revision) revision from public.summaries group by user_id,session_id
), versions as (
  select s.user_id,s.id,greatest(coalesce(e.revision,0),coalesce(sm.revision,0),coalesce(j.input_revision,0))+1 revision
  from public.sessions s
  left join event_versions e on e.user_id=s.user_id and e.session_id=s.id
  left join summary_versions sm on sm.user_id=s.user_id and sm.session_id=s.id
  left join private.summary_jobs j on j.user_id=s.user_id and j.session_id=s.id
)
update public.sessions s set summary_input_version=v.revision
from versions v where v.user_id=s.user_id and v.id=s.id;
alter table private.summary_jobs add column lease_token uuid;
alter table private.summary_jobs add column requested_explicitly boolean not null default false;
alter table public.rollup_dirty
  add column generation bigint not null default 1,
  add column claim_token uuid,
  add column claimed_until timestamptz;
-- Drain old workers before applying: their max(event.id) job revision no longer
-- describes the input. Preserve saved summaries, but rebind outstanding jobs.
update private.summary_jobs j set input_revision=s.summary_input_version,
  status=case when j.status='processing' then 'pending' else j.status end,
  locked_at=null,lease_token=null,attempts=0
from public.sessions s where s.user_id=j.user_id and s.id=j.session_id;

-- Keeps stale devices from resurrecting a deliberately deleted session.
create table private.deleted_sessions (
  user_id uuid not null references auth.users(id) on delete cascade,
  device_id uuid not null,
  source text not null,
  source_session_id text not null,
  deleted_at timestamptz not null default now(),
  primary key(user_id, device_id, source, source_session_id)
);
revoke all on private.deleted_sessions from public, anon, authenticated;
alter table private.deleted_sessions enable row level security;

create function private.remember_deleted_sessions() returns trigger
language plpgsql set search_path = pg_catalog as $$
begin
  insert into private.deleted_sessions(user_id,device_id,source,source_session_id)
  select o.user_id,o.device_id,o.source,o.source_session_id from old_sessions o
  join auth.users u on u.id=o.user_id
  on conflict do nothing;
  return null;
end;
$$;
create trigger sessions_deletion_tombstones after delete on public.sessions
referencing old table as old_sessions for each statement
execute function private.remember_deleted_sessions();
revoke all on function private.remember_deleted_sessions() from public, anon, authenticated;

-- Statement-level deletion safety also covers FK cascades. No per-event insert
-- counter trigger: ingestion coalesces version writes once per affected day.
create function private.invalidate_deleted_events() returns trigger
language plpgsql set search_path = pg_catalog as $$
declare affected record;
begin
  -- Match the normal writer order: sessions/jobs before shared day versions.
  update public.sessions s set summary_input_version=summary_input_version+1
    where (s.user_id,s.id) in (select user_id,session_id from old_events);
  delete from private.summary_jobs j using old_events o
    where j.user_id=o.user_id and j.session_id=o.session_id;
  delete from public.summaries sm using old_events o
    where sm.user_id=o.user_id and sm.session_id=o.session_id;
  for affected in
    select user_id, array_agg(distinct local_day order by local_day) as days
    from (
      select user_id,local_day from old_events
      union
      select e.user_id,e.local_day from public.events e
      join (select distinct user_id,session_id from old_events) o
        on o.user_id=e.user_id and o.session_id=e.session_id
    ) d group by user_id
  loop
    perform private.bump_day_versions(affected.user_id, affected.days, true,true,true,true,true);
    insert into public.rollup_dirty(user_id,local_day)
      select affected.user_id,d from unnest(affected.days) d
      where exists(select 1 from auth.users where id=affected.user_id)
      on conflict(user_id,local_day) do update set dirtied_at=now(),generation=public.rollup_dirty.generation+1;
    insert into public.dashboard_changes(user_id,local_day,change_kind)
      select affected.user_id,d,'events' from unnest(affected.days) d
      where exists(select 1 from auth.users where id=affected.user_id);
  end loop;
  return null;
end;
$$;
create trigger events_deletion_versions after delete on public.events
referencing old table as old_events for each statement
execute function private.invalidate_deleted_events();
revoke all on function private.invalidate_deleted_events() from public, anon, authenticated;

-- Applies to day, tooltip and rollup joins. Run production deployment during a
-- controlled migration window; benchmark write cost before enabling the API.
create index tool_calls_user_event_idx on public.tool_calls(user_id,event_id);
create index summaries_user_session_revision_idx
  on public.summaries(user_id,session_id,input_revision desc);

commit;
