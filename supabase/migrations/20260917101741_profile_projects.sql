begin;
set local lock_timeout='5s';
create table private.account_insights (
  user_id uuid primary key references auth.users(id) on delete cascade,
  timezone text not null default 'UTC',
  source_revision text,
  purge_revision bigint not null default 0,
  profile jsonb,
  projects jsonb not null default '[]',
  computed_at timestamptz,
  checked_at timestamptz not null default '1970-01-01',
  error text
);
alter table private.account_insights enable row level security;
revoke all on private.account_insights from public, anon, authenticated;

-- One row contains the last successful documents and the durable next-job state.
-- No request holds a database transaction open during a model call.
create table private.project_documents (
  user_id uuid not null references auth.users(id) on delete cascade,
  project_id uuid not null,
  project_name text not null,
  docs jsonb,
  input_revision text,
  purge_revision bigint not null default 0,
  state text not null default 'none' check(state in ('none','queued','running','ready','failed')),
  lease uuid,
  locked_at timestamptz,
  requested_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  requests_day date not null default current_date,
  requests_count integer not null default 0,
  answers jsonb not null default '{}',
  error text,
  primary key(user_id, project_id)
);
create index project_documents_jobs on private.project_documents(requested_at)
  where state in ('queued','running');
alter table private.project_documents enable row level security;
revoke all on private.project_documents from public, anon, authenticated;
insert into private.account_insights(user_id)
  select distinct user_id from public.sessions on conflict do nothing;
commit;
