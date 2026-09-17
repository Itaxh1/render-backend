begin;
set local lock_timeout='5s';
set local statement_timeout='120s';

alter table public.sessions add column display_title text,
  add column title_origin text check(title_origin in ('prompt','model')),
  add column title_prompt_at timestamptz;

-- Device/file identities are import aliases, not logical conversation identities.
-- NULL session_id is a durable tombstone: deleting a session does not free its keys.
create table private.session_identity_keys (
  user_id uuid not null references auth.users(id) on delete cascade,
  source text not null check(source in ('claude-code','codex')),
  identity_key text not null check(length(identity_key) between 1 and 512),
  session_id bigint,
  primary key(user_id,source,identity_key),
  foreign key(user_id,session_id) references public.sessions(user_id,id)
    on delete set null (session_id)
);
create index session_identity_target_idx on private.session_identity_keys(user_id,session_id);
alter table private.session_identity_keys enable row level security;
revoke all on private.session_identity_keys from public,anon,authenticated;
insert into private.session_identity_keys
select user_id,source,'device:'||device_id::text||':'||source_session_id,id from public.sessions;
insert into private.session_identity_keys
select user_id,source,'device:'||device_id::text||':'||source_session_id,null from private.deleted_sessions
on conflict do nothing;

-- Full-record hashes are scoped to a session AND record position. Repeated,
-- byte-identical turn_context records at different positions remain distinct.
create unique index events_session_record_identity_idx on public.events
  (user_id,session_id,source_sequence,source_item_index,payload_hash);

create function private.resolve_session(
  owner_id uuid,device uuid,product text,source_id text,
  source_title text,project text,model_name text,first_at timestamptz,last_at timestamptz,
  first_day date,record_revision integer,identity_keys text[]
) returns bigint language plpgsql set search_path=pg_catalog as $$
declare resolved bigint; targets bigint[]; has_deleted boolean;
begin
  -- Serialize only this account's ingest transactions, including two devices
  -- simultaneously reconnecting with the same history. No network call in lock.
  perform pg_advisory_xact_lock(hashtextextended(owner_id::text,742019));
  if not exists(select 1 from public.devices where user_id=owner_id and id=device) then
    raise exception 'device ownership mismatch';
  end if;
  select array_agg(distinct session_id) filter(where session_id is not null),bool_or(session_id is null)
    into targets,has_deleted from private.session_identity_keys
    where user_id=owner_id and source=product and identity_key=any(identity_keys);
  if has_deleted then
    insert into private.session_identity_keys(user_id,source,identity_key,session_id)
      select owner_id,product,k,null from unnest(identity_keys) k on conflict do nothing;
    return null;
  end if;
  if cardinality(targets)>1 then raise exception 'conflicting session identities'; end if;
  resolved:=targets[1];
  if resolved is null then
    insert into public.sessions(user_id,device_id,source,source_session_id,title,project_name,model,
      started_at,last_event_at,started_day,revision)
    values(owner_id,device,product,source_id,source_title,project,model_name,first_at,last_at,first_day,record_revision)
    returning id into resolved;
  else
    update public.sessions set started_at=least(started_at,first_at),
      started_day=least(started_day,first_day),last_event_at=greatest(last_event_at,last_at),
      title=coalesce(source_title,title),project_name=coalesce(project,project_name),
      model=coalesce(model_name,model),revision=greatest(revision,record_revision)
    where user_id=owner_id and id=resolved;
  end if;
  insert into private.session_identity_keys(user_id,source,identity_key,session_id)
    select owner_id,product,k,resolved from unnest(identity_keys) k on conflict do nothing;
  return resolved;
end;
$$;
revoke all on function private.resolve_session(uuid,uuid,text,text,text,text,text,timestamptz,timestamptz,date,integer,text[])
  from public,anon,authenticated;

create or replace function private.remember_deleted_sessions() returns trigger
language plpgsql set search_path=pg_catalog as $$
begin
  insert into private.deleted_sessions(user_id,device_id,source,source_session_id)
    select o.user_id,o.device_id,o.source,o.source_session_id from old_sessions o
    join auth.users u on u.id=o.user_id on conflict do nothing;
  insert into private.session_identity_keys(user_id,source,identity_key,session_id)
    select o.user_id,o.source,'device:'||o.device_id::text||':'||o.source_session_id,null
    from old_sessions o join auth.users u on u.id=o.user_id on conflict do nothing;
  return null;
end;
$$;
commit;
