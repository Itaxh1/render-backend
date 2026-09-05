import asyncio
from contextlib import asynccontextmanager
from uuid import uuid4

from backend import bulk_ingest
from backend.ingest_sql import EVENT_UPSERT, SESSION_UPSERT
from backend.models import DevicePrincipal, IngestBatch


class Cursor:
    def __init__(self, rows=()):
        self.rows = list(rows)

    async def fetchall(self):
        return self.rows


class Connection:
    def __init__(self, previous=()):
        self.previous = previous
        self.statements = []
        self.pipeline_depth = 0
        self.pipeline_stages = 0

    async def execute(self, sql, values=None):
        self.statements.append((sql, values, self.pipeline_depth))
        return Cursor(self.previous if "join jsonb_to_recordset" in sql else ())

    @asynccontextmanager
    async def pipeline(self):
        self.pipeline_stages += 1
        self.pipeline_depth += 1
        try:
            yield
        finally:
            self.pipeline_depth -= 1


def record(sequence, revision=1):
    return {
        "source": "codex", "source_file_id": "f" * 64, "sequence": sequence,
        "revision": revision, "payload_hash": "a" * 64, "stage": "enriched",
        "event": {
            "session_id": f"session-{sequence // 100}", "type": "user",
            "created_at": "2026-09-04T12:00:00Z", "local_day": "2026-09-04",
        },
    }


def batch(records):
    return IngestBatch(protocol_version=1, batch_id=uuid4(), device_sequence=1,
                       extractor_version=1, records=records)


def test_session_writes_are_grouped_and_per_event_operations_are_pipelined(monkeypatch):
    stages = []

    async def returning(connection, sql, values):
        stages.append((sql, values))
        return [{"id": i + 1} for i in range(len(values))]

    monkeypatch.setattr(bulk_ingest, "returning_rows", returning)
    connection = Connection()
    principal = DevicePrincipal(user_id=uuid4(), device_id=uuid4())
    assert asyncio.run(bulk_ingest.write_records(connection, principal, batch([
        record(i) for i in range(500)
    ]))) == (500, 0)
    assert [(sql, len(values)) for sql, values in stages] == [
        (SESSION_UPSERT, 5), (EVENT_UPSERT, 500),
    ]
    assert connection.pipeline_stages == 2
    event_operations = [s for s in connection.statements if "delete from public.tool_calls" in s[0]]
    assert len(event_operations) == 500
    assert all(depth == 1 for _, _, depth in event_operations)


def test_same_identity_in_a_batch_keeps_only_the_highest_revision(monkeypatch):
    stages = []

    async def returning(connection, sql, values):
        stages.append((sql, values))
        return [{"id": i + 1} for i in range(len(values))]

    monkeypatch.setattr(bulk_ingest, "returning_rows", returning)
    connection = Connection()
    principal = DevicePrincipal(user_id=uuid4(), device_id=uuid4())
    result = asyncio.run(bulk_ingest.write_records(connection, principal, batch([
        record(0, 2), record(0, 1), record(0, 2),
    ])))
    assert result == (1, 2)
    event_values = next(values for sql, values in stages if sql == EVENT_UPSERT)
    assert len(event_values) == 1
    assert event_values[0][6] == 2


def test_older_revisions_do_not_rewrite_events_or_enqueue_jobs():
    connection = Connection([{
        "source_file_id": "f" * 64, "source_sequence": 0, "source_item_index": 0,
        "revision": 3, "local_day": "2026-09-04",
    }])
    principal = DevicePrincipal(user_id=uuid4(), device_id=uuid4())
    assert asyncio.run(bulk_ingest.write_records(connection, principal, batch([record(0, 2)]))) == (0, 1)
    assert len(connection.statements) == 1
    assert connection.pipeline_stages == 0
