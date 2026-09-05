from __future__ import annotations

from copy import deepcopy
from uuid import UUID, uuid4

from fastapi import HTTPException
from fastapi.testclient import TestClient

from backend.app import create_app
from backend.config import Settings
from backend.store import MemoryStore


USER_ID = uuid4()


class FakeVerifier:
    async def verify(self, token: str) -> UUID:
        if token != "browser-token":
            raise HTTPException(status_code=401, detail="invalid access token")
        return USER_ID


def make_app():
    settings = Settings(
        database_url="postgresql://unused",
        supabase_url="https://example.supabase.co",
        rexy_web_origin="http://localhost:5173",
        supabase_publishable_key="publishable-test-key",
    )
    return create_app(settings, MemoryStore(), FakeVerifier())


def connect_device(client: TestClient) -> tuple[str, str]:
    response = client.post(
        "/v1/install/claims", headers={"authorization": "Bearer browser-token"}
    )
    assert response.status_code == 201
    claim = response.json()["claim_token"]
    response = client.post(
        "/v1/devices/exchange-claim",
        json={"claim_token": claim, "device_name": "Test Mac", "platform": "darwin"},
    )
    assert response.status_code == 201
    return response.json()["device_id"], response.json()["device_token"]


def event_record(*, revision: int = 1) -> dict:
    return {
        "source": "codex",
        "source_file_id": "a" * 64,
        "sequence": 12,
        "revision": revision,
        "payload_hash": "b" * 64,
        "event": {
            "session_id": "session-1",
            "type": "tool",
            "role": None,
            "created_at": "2026-09-04T12:00:00Z",
            "local_day": "2026-09-04",
            "tool_name": "Bash",
            "tool_status": "succeeded",
            "source_bytes": 123,
        },
    }


def batch(record: dict, *, batch_id: str | None = None, device_sequence: int = 1) -> dict:
    return {
        "protocol_version": 1,
        "batch_id": batch_id or str(uuid4()),
        "device_sequence": device_sequence,
        "extractor_version": 1,
        "records": [record],
    }


def test_claim_is_single_use_and_token_types_are_separate() -> None:
    app = make_app()
    with TestClient(app) as client:
        response = client.post(
            "/v1/install/claims", headers={"authorization": "Bearer browser-token"}
        )
        claim = response.json()["claim_token"]
        first = client.post(
            "/v1/devices/exchange-claim",
            json={"claim_token": claim, "device_name": "Test Mac", "platform": "darwin"},
        )
        assert first.status_code == 201
        second = client.post(
            "/v1/devices/exchange-claim",
            json={"claim_token": claim, "device_name": "Again", "platform": "darwin"},
        )
        assert second.status_code == 400

        device_token = first.json()["device_token"]
        assert client.post(
            "/v1/install/claims", headers={"authorization": f"Bearer {device_token}"}
        ).status_code == 401
        assert client.post(
            "/v1/ingest/batches",
            headers={"authorization": "Bearer browser-token"},
            json=batch(event_record()),
        ).status_code == 401


def test_ingestion_is_idempotent_and_accepts_newer_revisions() -> None:
    app = make_app()
    with TestClient(app) as client:
        _, token = connect_device(client)
        headers = {"authorization": f"Bearer {token}"}
        first_batch = batch(event_record(), device_sequence=1)
        first = client.post("/v1/ingest/batches", headers=headers, json=first_batch)
        assert first.status_code == 200
        assert first.json()["accepted"] == 1

        retry = client.post("/v1/ingest/batches", headers=headers, json=first_batch)
        assert retry.status_code == 200
        assert retry.json() == first.json()

        duplicate = client.post(
            "/v1/ingest/batches",
            headers=headers,
            json=batch(event_record(), device_sequence=2),
        )
        assert duplicate.status_code == 200
        assert duplicate.json()["duplicate"] == 1

        revision = client.post(
            "/v1/ingest/batches",
            headers=headers,
            json=batch(event_record(revision=2), device_sequence=3),
        )
        assert revision.status_code == 200
        assert revision.json()["accepted"] == 1


def test_reusing_batch_id_with_different_content_is_rejected() -> None:
    app = make_app()
    with TestClient(app) as client:
        _, token = connect_device(client)
        headers = {"authorization": f"Bearer {token}"}
        original = batch(event_record())
        assert client.post("/v1/ingest/batches", headers=headers, json=original).status_code == 200
        changed = deepcopy(original)
        changed["records"][0]["sequence"] = 13
        response = client.post("/v1/ingest/batches", headers=headers, json=changed)
        assert response.status_code == 409

        reused_sequence = batch(event_record(), device_sequence=original["device_sequence"])
        response = client.post(
            "/v1/ingest/batches", headers=headers, json=reused_sequence
        )
        assert response.status_code == 409


def test_schema_limits_and_readiness() -> None:
    app = make_app()
    with TestClient(app) as client:
        assert client.get("/healthz").json() == {"status": "ok"}
        assert client.get("/readyz").json() == {"status": "ready"}
        _, token = connect_device(client)
        invalid = batch(event_record())
        invalid["records"][0]["payload_hash"] = "not-a-hash"
        response = client.post(
            "/v1/ingest/batches",
            headers={"authorization": f"Bearer {token}"},
            json=invalid,
        )
        assert response.status_code == 422


def test_browser_dashboard_returns_ingested_events() -> None:
    app = make_app()
    with TestClient(app) as client:
        assert client.get("/v1/public/config").json() == {
            "supabase_url": "https://example.supabase.co",
            "supabase_publishable_key": "publishable-test-key",
        }
        _, token = connect_device(client)
        response = client.post(
            "/v1/ingest/batches",
            headers={"authorization": f"Bearer {token}"},
            json=batch(event_record()),
        )
        assert response.status_code == 200

        dashboard = client.get(
            "/v1/dashboard?year=2026",
            headers={"authorization": "Bearer browser-token"},
        )
        assert dashboard.status_code == 200
        payload = dashboard.json()
        assert payload["events"][0]["k"] == "tool"
        assert payload["events"][0]["st"] == "succeeded"
        assert payload["rollups"]["2026-09-04"]["codex"]["tools"] == 1
        assert payload["rollups"]["2026-09-04"]["codex"]["ok"] == 1

        empty_day = client.get(
            "/v1/dashboard?year=2026&day=2026-09-03",
            headers={"authorization": "Bearer browser-token"},
        )
        assert empty_day.status_code == 200
        assert empty_day.json()["events"] == []
        assert empty_day.json()["stats"]["strokes"] == 1

        mismatched_day = client.get(
            "/v1/dashboard?year=2025&day=2026-09-04",
            headers={"authorization": "Bearer browser-token"},
        )
        assert mismatched_day.status_code == 422

        denied = client.get(
            "/v1/dashboard?year=2026",
            headers={"authorization": f"Bearer {token}"},
        )
        assert denied.status_code == 401
