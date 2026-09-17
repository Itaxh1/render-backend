import asyncio
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
import os
from types import SimpleNamespace
from uuid import UUID

import psycopg
from psycopg.rows import dict_row
import pytest

from backend.day_queries import RIBBON_EVENTS_SQL
from backend.postgres import PostgresStore

OWNER, OTHER = UUID(int=1), UUID(int=2)
DAY = date(2026, 9, 16)


def test_legacy_dashboard_uses_bounded_ribbon_without_extra_roundtrips():
    statements = []

    async def execute(query, values):
        statements.append((query, values))

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
    asyncio.run(store.dashboard(OWNER, 2026, DAY, detail_only=True))
    assert (RIBBON_EVENTS_SQL, (OWNER, DAY, OWNER)) in statements
    assert len(statements) == 5
    assert 'limit ' not in RIBBON_EVENTS_SQL.lower()


def test_ribbon_keeps_unknown_durations_late_results_and_owner_isolation():
    dsn = os.environ.get('REXY_TEST_DATABASE_URL')
    if not dsn:
        pytest.skip('Set REXY_TEST_DATABASE_URL for the SQL regression')
    with psycopg.connect(dsn, row_factory=dict_row) as db:
        db.execute('''
            create temp table events (id bigint, user_id uuid, session_id bigint,
                created_at timestamptz, local_day date, type text);
            create temp table sessions (id bigint, user_id uuid, source text);
            create temp table tool_calls (event_id bigint, user_id uuid, session_id bigint,
                status text, tool_name text, duration_ms bigint);
        ''')
        db.execute("insert into pg_temp.sessions values (1,%s,'codex'),(2,%s,'claude-code'),(1,%s,'claude-code')",
                   (OWNER, OWNER, OTHER))
        moment = datetime(2026, 9, 16, 14, tzinfo=timezone.utc)
        for eid, sid, owner, day, kind in [
            (1,1,OWNER,DAY,'user'), (2,1,OWNER,DAY,'tool'),
            (3,2,OWNER,DAY,'tool'), (4,1,OWNER,DAY,'tool_result'),
            (5,1,OWNER,DAY,'usage'), (6,1,OWNER,date(2026,9,15),'agent'),
            (7,1,OTHER,DAY,'user'),
        ]:
            db.execute('insert into pg_temp.events values (%s,%s,%s,%s,%s,%s)',
                       (eid,owner,sid,moment,day,kind))
        db.execute("insert into pg_temp.tool_calls values (2,%s,1,'failed','exec',null),(3,%s,2,'succeeded','Read',0),(2,%s,1,'succeeded','foreign',9)",
                   (OWNER, OWNER, OTHER))
        rows = db.execute(RIBBON_EVENTS_SQL.replace('public.', 'pg_temp.'),
                          (OWNER,DAY,OWNER)).fetchall()
        assert [r['id'] for r in rows] == [1,2,3]
        assert rows[1]['status'] == 'failed' and rows[1]['duration_ms'] is None
        assert rows[2]['duration_ms'] == 0
        assert db.execute(RIBBON_EVENTS_SQL.replace('public.', 'pg_temp.'),
                          (OWNER,date(2026,9,17),OWNER)).fetchall() == []
        db.rollback()
