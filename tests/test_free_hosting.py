import asyncio
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from backend.app import create_app
from backend.config import Settings, WorkerSettings
from backend.store import MemoryStore
from backend.worker import run_embedded_worker


SETTINGS = Settings(database_url="postgresql://unused", supabase_url="https://example.supabase.co",
                    rexy_web_origin="http://localhost:5173")


def test_embedded_worker_does_not_block_readiness_and_stops_on_shutdown(monkeypatch):
    states = []

    async def run(settings):
        assert settings.xai_model == "grok-4.3"
        states.append("started")
        try:
            await asyncio.Event().wait()
        finally:
            states.append("stopped")

    monkeypatch.setattr("backend.app.run_embedded_worker", run)
    with TestClient(create_app(replace(SETTINGS, embedded_summaries=True, xai_api_key="test"), MemoryStore())) as client:
        assert client.get("/readyz").json() == {"status": "ready"}
        assert states == ["started"]
    assert states == ["started", "stopped"]


def test_separate_worker_mode_remains_the_default(monkeypatch):
    run = AsyncMock()
    monkeypatch.setattr("backend.app.run_embedded_worker", run)
    with TestClient(create_app(SETTINGS, MemoryStore())) as client:
        assert client.get("/healthz").status_code == 200
    run.assert_not_called()


def test_embedded_mode_requires_the_server_key(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", SETTINGS.database_url)
    monkeypatch.setenv("SUPABASE_URL", SETTINGS.supabase_url)
    monkeypatch.setenv("REXY_EMBED_SUMMARIES", "1")
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="XAI_API_KEY is required"):
        Settings.from_environment()
    monkeypatch.setenv("XAI_API_KEY", "server-only-test")
    assert Settings.from_environment().embedded_summaries


def test_worker_recovers_from_database_outage_and_closes_on_cancellation(monkeypatch, caplog):
    instances = []
    sleeps = []

    class Worker:
        def __init__(self, settings):
            self.open = AsyncMock(side_effect=RuntimeError("private database URL"))
            self.run = AsyncMock()
            self.close = AsyncMock()
            instances.append(self)

    async def sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 2:
            raise asyncio.CancelledError()

    monkeypatch.setattr("backend.worker.SummaryWorker", Worker)
    monkeypatch.setattr("backend.worker.asyncio.sleep", sleep)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(run_embedded_worker(WorkerSettings("postgresql://unused", "test")))
    assert len(instances) == 2
    assert sleeps == [5, 5]
    for instance in instances:
        instance.close.assert_awaited_once()
        instance.run.assert_not_called()
    assert "private database URL" not in caplog.text


def test_worker_cancellation_closes_pool_without_retry(monkeypatch):
    worker = AsyncMock()
    worker.run.side_effect = asyncio.CancelledError()
    monkeypatch.setattr("backend.worker.SummaryWorker", lambda _: worker)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(run_embedded_worker(WorkerSettings("postgresql://unused", "test")))
    worker.close.assert_awaited_once()
