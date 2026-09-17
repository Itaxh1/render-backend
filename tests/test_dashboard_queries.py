"""Exercise the emitted SQL against isolated temporary PostgreSQL tables.

Set REXY_TEST_DATABASE_URL to a disposable test database to run SQL cases.
The normal unit suite still checks query bounds and parameter ordering.
"""
import asyncio
import os
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
from types import SimpleNamespace
from uuid import UUID

import psycopg
import pytest
from psycopg.rows import dict_row

from backend.postgres import PostgresStore

OWNER = UUID(int=1)
OTHER = UUID(int=2)
DAY = date(2026, 9, 4)


def dashboard_queries(day=DAY, detail_only=True):
    statements = []

    async def execute(sql, args):
        statements.append((sql, args))

        async def fetchall():
            return []

        async def fetchone():
            return dict(files=0, source_bytes=0, strokes=0)

        return SimpleNamespace(fetchall=fetchall, fetchone=fetchone)

    @asynccontextmanager
    async def connection():
        yield SimpleNamespace(execute=execute)

    store = PostgresStore.__new__(PostgresStore)
    store._pool = SimpleNamespace(connection=connection)
    asyncio.run(store.dashboard(OWNER, day.year, day, detail_only=detail_only))
    return {name: next((sql, args) for sql, args in statements if marker in sql)
            for name, marker in [('sessions', 'summary.tldr'), ('tokens', 'previous_input')]}


def test_queries_bind_active_sessions_before_decorating_or_calculating_deltas():
    queries = dashboard_queries()
    sql, args = queries['sessions']
    assert args == (OWNER, DAY, DAY, OWNER)
    assert 'from active a' in sql
    assert 's.user_id = %s and s.id = a.session_id' in sql
    assert 'selected_event' not in sql
    sql, args = queries['tokens']
    assert args == (OWNER, DAY, date(2026, 9, 5), OWNER, DAY, date(2026, 9, 5))
    assert 'e.session_id = any(array(select session_id from active))' in sql
    # No date predicate inside samples: its predecessor can be on another day.
    assert 'local_day >=' not in sql.split('), samples as (')[1].split('), deltas as (')[0]


def test_legacy_dashboard_bounds_tokens_by_year_not_selected_day():
    _, args = dashboard_queries(detail_only=False)['tokens']
    assert args == (OWNER, date(2026, 1, 1), date(2027, 1, 1),
                    OWNER, date(2026, 1, 1), date(2027, 1, 1))


@pytest.fixture
def db():
    dsn = os.environ.get('REXY_TEST_DATABASE_URL')
    if not dsn:
        pytest.skip('Set REXY_TEST_DATABASE_URL for PostgreSQL query regression tests')
    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        conn.execute("""
            create temp table sessions (
                id bigint, user_id uuid, source text, title text, project_name text,
                model text, started_at timestamptz, ended_at timestamptz, last_event_at timestamptz,
                display_title text
            );
            create temp table events (
                id bigint, user_id uuid, session_id bigint, local_day date, created_at timestamptz,
                type text, content_preview text, usage_cumulative boolean,
                token_input bigint, token_output bigint, token_cache_read bigint,
                token_cache_write bigint, token_thinking bigint
            );
            create temp table summaries (user_id uuid, session_id bigint, input_revision bigint, tldr text);
            create temp table summary_jobs (user_id uuid, session_id bigint, status text);
        """)
        yield conn
        conn.rollback()


def session(db, sid, owner=OWNER, source='codex', title=None):
    db.execute("""insert into pg_temp.sessions values
        (%s, %s, %s, %s, 'project', 'model', '2025-12-31 00:00:00+00', null, '2026-09-04 00:00:00+00',null)""",
        (sid, owner, source, title))


def event(db, eid, sid, day=DAY, owner=OWNER, kind='user', text='Goal', usage=None, cumulative=True):
    values = usage or (None,) * 5
    db.execute('insert into pg_temp.events values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)',
               (eid, owner, sid, day, datetime.combine(day, datetime.min.time(), timezone.utc),
                kind, text, cumulative, *values))


def run(db, name, day=DAY, detail_only=True):
    sql, args = dashboard_queries(day, detail_only)[name]
    # The exact application query/parameters, with table names redirected to
    # this connection's temporary schema. No application table is modified.
    sql = sql.replace('public.', 'pg_temp.').replace('private.', 'pg_temp.')
    return db.execute(sql, args).fetchall()


def test_day_sessions_are_distinct_owned_and_keep_latest_saved_summary(db):
    session(db, 1)
    session(db, 2)
    session(db, 1, OTHER, title='Foreign session')
    event(db, 1, 1, day=date(2026, 9, 3), text='Original goal')
    db.execute("update pg_temp.sessions set display_title='Original goal' where user_id=%s and id=1", (OWNER,))
    event(db, 2, 1, kind='tool', text=None)
    event(db, 3, 1, kind='agent', text='Result')
    event(db, 4, 2, day=date(2026, 9, 2))
    event(db, 5, 1, owner=OTHER, text='Foreign prompt')
    db.execute("insert into pg_temp.summaries values (%s,1,1,'Old TLDR'),(%s,1,2,'Saved TLDR'),(%s,1,99,'Foreign TLDR')",
               (OWNER, OWNER, OTHER))
    db.execute("insert into pg_temp.summary_jobs values (%s,1,'processing')", (OWNER,))
    rows = run(db, 'sessions')
    assert len(rows) == 1
    assert (rows[0]['id'], rows[0]['title'], rows[0]['tldr'], rows[0]['summary_state']) == (
        1, 'Original goal', 'Saved TLDR', 'ready')
    assert rows[0]['display_day'] == DAY


def test_cumulative_tokens_keep_midnight_predecessor_duplicates_and_counter_resets(db):
    session(db, 1)
    event(db, 1, 1, day=date(2026, 9, 3), usage=(100, 40, 60, 5, 10))
    event(db, 2, 1, usage=(150, 70, 80, 9, 20))
    event(db, 3, 1, usage=(150, 70, 80, 9, 20))
    event(db, 4, 1, usage=(10, 5, 6, 2, 1))
    session(db, 2)
    event(db, 5, 2, day=date(2026, 9, 2), usage=(999, 999, 999, 999, 999))
    session(db, 1, OTHER)
    event(db, 6, 1, owner=OTHER, usage=(999, 999, 999, 999, 999))
    rows = run(db, 'tokens')
    assert len(rows) == 1
    assert [rows[0][key] for key in ('token_input', 'token_output', 'token_cache_read',
                                   'token_cache_write', 'token_thinking')] == [60, 35, 26, 6, 11]


def test_non_cumulative_usage_and_first_sample_remain_intact(db):
    session(db, 1, source='claude-code')
    event(db, 1, 1, usage=(10, 20, 30, 5, 12), cumulative=False)
    event(db, 2, 1, usage=(10, 20, 30, 5, 12), cumulative=False)
    session(db, 2)
    event(db, 3, 2, usage=(50, 10, 20, 0, 3))
    rows = {r['source']: r for r in run(db, 'tokens')}
    assert rows['claude-code']['token_input'] == 20
    assert rows['claude-code']['token_output'] == 40
    assert rows['codex']['token_input'] == 50


def test_year_query_keeps_other_days_and_prior_year_predecessor(db):
    session(db, 1)
    event(db, 1, 1, day=date(2025, 12, 31), usage=(100, 40, 60, 0, 10))
    event(db, 2, 1, day=date(2026, 1, 1), usage=(150, 70, 80, 0, 20))
    session(db, 2)
    event(db, 3, 2, usage=(20, 10, 5, 0, 3))
    rows = run(db, 'tokens', detail_only=False)
    assert [(r['local_day'], r['token_input']) for r in rows] == [(date(2026, 1, 1), 50), (DAY, 20)]
    assert run(db, 'tokens', day=date(2026, 1, 1))[0]['token_input'] == 50


def test_empty_day_does_not_leak_history_or_invent_usage(db):
    session(db, 1)
    event(db, 1, 1, day=date(2026, 9, 2), usage=(100, 20, 30, 0, 3))
    assert run(db, 'sessions') == []
    assert run(db, 'tokens') == []
