import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

from backend.worker import Job, SummaryOutput, SummaryWorker


class Connection:
    def __init__(self):
        self.statements = []

    @asynccontextmanager
    async def transaction(self):
        yield

    async def execute(self, sql, args=None):
        self.statements.append((sql, args))
        async def fetchone():
            return None
        return SimpleNamespace(fetchone=fetchone)


def make_worker():
    connection = Connection()
    @asynccontextmanager
    async def borrow():
        yield connection
    worker = SummaryWorker.__new__(SummaryWorker)
    worker._pool = SimpleNamespace(connection=borrow)
    worker._settings = SimpleNamespace(xai_model='grok-4.3')
    return worker, connection


def test_queue_prioritizes_recent_activity_with_owner_join_and_skip_locked():
    worker, connection = make_worker()
    asyncio.run(worker.claim())
    sql = connection.statements[0][0]
    assert 's.user_id = j.user_id' in sql
    assert 'order by s.last_event_at desc nulls last' in sql
    assert 'for update of j skip locked' in sql
    assert 'j.available_at <= now()' in sql


def test_saved_summary_revision_cannot_be_overwritten_by_duplicate_completion():
    worker, connection = make_worker()
    job = Job(id=1, user_id='owner', session_id=2, input_revision=3, lease_token='lease')
    asyncio.run(worker._save_summary(connection, job, SummaryOutput(tldr='Saved output.', outcome='completed')))
    sql = connection.statements[0][0]
    assert 'on conflict (user_id, session_id, input_revision) do nothing' in sql
    assert 'delete' not in sql.lower()
    assert connection.statements[0][1][6] == 'grok-4.3'


def test_old_failure_does_not_mark_new_revision_failed():
    worker, connection = make_worker()
    asyncio.run(worker.fail(Job(id=1, user_id='owner', session_id=2, input_revision=3, lease_token='lease'), ValueError('secret error body')))
    sql, args = connection.statements[1]
    assert 'lease_token=%s' in sql
    assert args[-4:] == (1, 'owner', 2, 'lease')
    assert 'secret error body' not in str(args)
