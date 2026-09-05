begin;

create extension if not exists pgcrypto;
create schema if not exists private;

revoke all on schema private from public, anon, authenticated;

create table public.devices (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  name text not null check (char_length(name) between 1 and 120),
  platform text not null check (char_length(platform) between 1 and 64),
  token_hash bytea not null unique check (octet_length(token_hash) = 32),
  token_expires_at timestamptz not null,
  extractor_version integer not null default 1 check (extractor_version > 0),
  created_at timestamptz not null default now(),
  last_seen_at timestamptz,
  revoked_at timestamptz,
  unique (user_id, id)
);

create table private.device_claims (
  id bigint generated always as identity primary key,
  user_id uuid not null references auth.users(id) on delete cascade,
  claim_hash bytea not null unique check (octet_length(claim_hash) = 32),
  expires_at timestamptz not null,
  consumed_at timestamptz,
  attempts integer not null default 0 check (attempts >= 0),
  created_at timestamptz not null default now()
);

create table public.sessions (
  id bigint generated always as identity primary key,
  user_id uuid not null,
  device_id uuid not null,
  source text not null check (source in ('claude-code', 'codex')),
  source_session_id text not null check (char_length(source_session_id) between 1 and 256),
  title text,
  model text,
  started_at timestamptz not null,
  last_event_at timestamptz not null,
  ended_at timestamptz,
  started_day date not null,
  status text not null default 'unknown'
    check (status in ('unknown', 'active', 'completed', 'interrupted', 'abandoned')),
  revision integer not null default 1 check (revision > 0),
  unique (user_id, device_id, source, source_session_id),
  unique (user_id, id),
  constraint sessions_device_owner_fk
    foreign key (user_id, device_id)
    references public.devices(user_id, id) on delete cascade
);

create table public.events (
  id bigint generated always as identity primary key,
  user_id uuid not null,
  device_id uuid not null,
  session_id bigint not null,
  source_file_id text not null check (char_length(source_file_id) between 32 and 128),
  source_sequence bigint not null check (source_sequence >= 0),
  revision integer not null check (revision > 0),
  payload_hash bytea not null check (octet_length(payload_hash) = 32),
  type text not null check (type in ('user', 'agent', 'tool')),
  role text check (role is null or role in ('user', 'assistant')),
  content_preview text check (
    content_preview is null or octet_length(content_preview) <= 8192
  ),
  created_at timestamptz not null,
  local_day date not null,
  parse_status text not null default 'parsed'
    check (parse_status in ('skeleton', 'parsed', 'failed')),
  truncated boolean not null default false,
  source_bytes bigint not null default 0 check (source_bytes >= 0),
  unique (device_id, source_file_id, source_sequence),
  unique (user_id, id),
  unique (user_id, session_id, id),
  constraint events_device_owner_fk
    foreign key (user_id, device_id)
    references public.devices(user_id, id) on delete cascade,
  constraint events_session_owner_fk
    foreign key (user_id, session_id)
    references public.sessions(user_id, id) on delete cascade
);

create table public.tool_calls (
  id bigint generated always as identity primary key,
  user_id uuid not null,
  session_id bigint not null,
  event_id bigint not null,
  source_call_id text not null,
  revision integer not null check (revision > 0),
  tool_name text not null check (char_length(tool_name) between 1 and 128),
  status text not null default 'unknown'
    check (status in ('unknown', 'running', 'succeeded', 'failed', 'interrupted', 'canceled')),
  input_preview text check (input_preview is null or octet_length(input_preview) <= 8192),
  output_preview text check (output_preview is null or octet_length(output_preview) <= 8192),
  input_size bigint check (input_size is null or input_size >= 0),
  output_size bigint check (output_size is null or output_size >= 0),
  input_hash bytea check (input_hash is null or octet_length(input_hash) = 32),
  output_hash bytea check (output_hash is null or octet_length(output_hash) = 32),
  exit_code integer,
  started_at timestamptz not null,
  ended_at timestamptz,
  duration_ms bigint check (duration_ms is null or duration_ms >= 0),
  local_day date not null,
  unique (user_id, session_id, source_call_id),
  unique (user_id, id),
  constraint tool_calls_event_owner_fk
    foreign key (user_id, session_id, event_id)
    references public.events(user_id, session_id, id) on delete cascade,
  constraint tool_calls_session_owner_fk
    foreign key (user_id, session_id)
    references public.sessions(user_id, id) on delete cascade
);

create table public.rollup_dirty (
  user_id uuid not null references auth.users(id) on delete cascade,
  local_day date not null,
  dirtied_at timestamptz not null default now(),
  primary key (user_id, local_day)
);

create table public.dashboard_changes (
  sequence bigint generated always as identity primary key,
  user_id uuid not null references auth.users(id) on delete cascade,
  local_day date not null,
  session_id bigint,
  change_kind text not null check (change_kind in ('events', 'session', 'device', 'summary')),
  created_at timestamptz not null default now(),
  constraint dashboard_changes_session_owner_fk
    foreign key (user_id, session_id)
    references public.sessions(user_id, id) on delete cascade
);

create table public.device_imports (
  user_id uuid not null,
  device_id uuid not null,
  total_bytes_discovered bigint not null default 0 check (total_bytes_discovered >= 0),
  tier0_bytes_scanned bigint not null default 0 check (tier0_bytes_scanned >= 0),
  bytes_queued bigint not null default 0 check (bytes_queued >= 0),
  bytes_acked bigint not null default 0 check (bytes_acked >= 0),
  current_file_bytes_scanned bigint not null default 0 check (current_file_bytes_scanned >= 0),
  current_file_total_bytes bigint not null default 0 check (current_file_total_bytes >= 0),
  files_discovered integer not null default 0 check (files_discovered >= 0),
  files_completed integer not null default 0 check (files_completed >= 0),
  updated_at timestamptz not null default now(),
  primary key (user_id, device_id),
  constraint device_imports_device_owner_fk
    foreign key (user_id, device_id)
    references public.devices(user_id, id) on delete cascade
);

create table private.ingest_batches (
  id bigint generated always as identity primary key,
  user_id uuid not null,
  device_id uuid not null,
  batch_id uuid not null,
  device_sequence bigint not null check (device_sequence > 0),
  request_hash bytea not null check (octet_length(request_hash) = 32),
  accepted integer not null check (accepted >= 0),
  duplicate integer not null check (duplicate >= 0),
  rejected integer not null check (rejected >= 0),
  receipt jsonb not null,
  created_at timestamptz not null default now(),
  unique (device_id, batch_id),
  unique (device_id, device_sequence),
  constraint ingest_batches_device_owner_fk
    foreign key (user_id, device_id)
    references public.devices(user_id, id) on delete cascade
);

create index devices_user_last_seen_idx
  on public.devices (user_id, last_seen_at desc);
create index sessions_user_last_event_idx
  on public.sessions (user_id, last_event_at desc);
create index sessions_device_idx
  on public.sessions (user_id, device_id);
create index events_user_day_time_idx
  on public.events (user_id, local_day, created_at, id);
create index events_session_time_idx
  on public.events (user_id, session_id, created_at, id);
create index tool_calls_user_day_status_idx
  on public.tool_calls (user_id, local_day, status);
create index tool_calls_session_time_idx
  on public.tool_calls (user_id, session_id, started_at, id);
create index dashboard_changes_user_sequence_idx
  on public.dashboard_changes (user_id, sequence);
create index device_claims_expiry_idx
  on private.device_claims (expires_at)
  where consumed_at is null;

alter table public.devices enable row level security;
alter table public.sessions enable row level security;
alter table public.events enable row level security;
alter table public.tool_calls enable row level security;
alter table public.rollup_dirty enable row level security;
alter table public.dashboard_changes enable row level security;
alter table public.device_imports enable row level security;
alter table private.device_claims enable row level security;
alter table private.ingest_batches enable row level security;

create policy devices_read_own on public.devices
  for select to authenticated
  using ((select auth.uid()) = user_id);
create policy sessions_read_own on public.sessions
  for select to authenticated
  using ((select auth.uid()) = user_id);
create policy events_read_own on public.events
  for select to authenticated
  using ((select auth.uid()) = user_id);
create policy tool_calls_read_own on public.tool_calls
  for select to authenticated
  using ((select auth.uid()) = user_id);
create policy device_imports_read_own on public.device_imports
  for select to authenticated
  using ((select auth.uid()) = user_id);

revoke all on public.devices, public.sessions, public.events,
  public.tool_calls, public.rollup_dirty, public.dashboard_changes,
  public.device_imports from anon, authenticated;
revoke all on sequence public.sessions_id_seq, public.events_id_seq,
  public.tool_calls_id_seq, public.dashboard_changes_sequence_seq
  from anon, authenticated;
grant select on public.devices, public.sessions, public.events,
  public.tool_calls, public.device_imports to authenticated;

commit;
