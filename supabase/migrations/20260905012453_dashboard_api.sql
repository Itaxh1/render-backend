begin;

alter table public.sessions
  add column project_name text;

alter table public.events
  drop constraint events_type_check,
  drop constraint events_device_id_source_file_id_source_sequence_key,
  add column source_item_index integer not null default 0
    check (source_item_index >= 0),
  add column model text,
  add column source_call_id text,
  add column token_input bigint check (token_input is null or token_input >= 0),
  add column token_output bigint check (token_output is null or token_output >= 0),
  add column token_cache_read bigint check (token_cache_read is null or token_cache_read >= 0),
  add column token_cache_write bigint check (token_cache_write is null or token_cache_write >= 0),
  add column token_thinking bigint check (token_thinking is null or token_thinking >= 0),
  add column usage_cumulative boolean not null default false,
  add constraint events_type_check
    check (type in ('user', 'agent', 'tool', 'tool_result', 'usage')),
  add constraint events_source_record_key
    unique (device_id, source_file_id, source_sequence, source_item_index);

create table public.daily_rollups (
  user_id uuid not null references auth.users(id) on delete cascade,
  local_day date not null,
  source text not null check (source in ('claude-code', 'codex')),
  sessions integer not null check (sessions >= 0),
  events integer not null check (events >= 0),
  tools integer not null check (tools >= 0),
  succeeded integer not null check (succeeded >= 0),
  failed integer not null check (failed >= 0),
  updated_at timestamptz not null default now(),
  primary key (user_id, local_day, source)
);

create table public.summaries (
  id bigint generated always as identity primary key,
  user_id uuid not null,
  session_id bigint not null,
  input_revision bigint not null check (input_revision > 0),
  tldr text not null check (char_length(tldr) between 1 and 500),
  outcome text not null
    check (outcome in ('completed', 'partial', 'abandoned', 'unknown')),
  unresolved text check (unresolved is null or char_length(unresolved) <= 500),
  model text not null,
  prompt_version integer not null default 1 check (prompt_version > 0),
  generated_at timestamptz not null default now(),
  unique (user_id, session_id, input_revision),
  unique (user_id, id),
  constraint summaries_session_owner_fk
    foreign key (user_id, session_id)
    references public.sessions(user_id, id) on delete cascade
);

create table private.summary_jobs (
  id bigint generated always as identity primary key,
  user_id uuid not null,
  session_id bigint not null,
  input_revision bigint not null check (input_revision > 0),
  status text not null default 'pending'
    check (status in ('pending', 'processing', 'completed', 'failed')),
  attempts integer not null default 0 check (attempts >= 0),
  available_at timestamptz not null default now(),
  locked_at timestamptz,
  last_error text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (user_id, session_id),
  constraint summary_jobs_session_owner_fk
    foreign key (user_id, session_id)
    references public.sessions(user_id, id) on delete cascade
);

create index events_user_usage_time_idx
  on public.events (user_id, session_id, created_at, id)
  where token_input is not null or token_output is not null;
create index summaries_user_session_generated_idx
  on public.summaries (user_id, session_id, generated_at desc);
create index summary_jobs_pending_idx
  on private.summary_jobs (available_at, id)
  where status = 'pending';
create index summary_jobs_session_idx
  on private.summary_jobs (user_id, session_id);

alter table public.daily_rollups enable row level security;
alter table public.summaries enable row level security;
alter table private.summary_jobs enable row level security;

create policy daily_rollups_read_own on public.daily_rollups
  for select to authenticated
  using ((select auth.uid()) = user_id);
create policy summaries_read_own on public.summaries
  for select to authenticated
  using ((select auth.uid()) = user_id);

revoke all on public.daily_rollups, public.summaries from anon, authenticated;
revoke all on private.summary_jobs from public, anon, authenticated;
revoke all on sequence public.summaries_id_seq, private.summary_jobs_id_seq
  from public, anon, authenticated;
grant select on public.daily_rollups, public.summaries to authenticated;

commit;
