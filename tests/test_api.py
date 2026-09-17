from __future__ import annotations

from copy import deepcopy
from uuid import UUID, uuid4

from fastapi import HTTPException
from fastapi.testclient import TestClient

from backend.app import create_app
from backend.config import Settings
from backend.store import MemoryStore


USER_ID = uuid4()
OTHER_USER_ID = uuid4()


class FakeVerifier:
    async def verify(self, token: str) -> UUID:
        if token == "other-browser-token":
            return OTHER_USER_ID
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


def test_storage_failure_is_retryable_and_does_not_leak_database_errors(monkeypatch):
    from psycopg.errors import DiskFull
    async def failed_claim(self, owner):
        raise DiskFull('private storage path and details')
    monkeypatch.setattr(MemoryStore,'create_claim',failed_claim)
    with TestClient(make_app()) as client:
        response=client.post('/v1/install/claims',headers={'authorization':'Bearer browser-token'})
        assert response.status_code==503
        assert response.headers['Retry-After']=='60'
        assert 'saved history is retained' in response.json()['detail']
        assert 'private storage' not in response.text


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


def test_event_previews_are_owned_and_device_tokens_cannot_read_them() -> None:
    with TestClient(make_app()) as client:
        _, token = connect_device(client)
        record = event_record()
        record["event"].update(tool_input_preview="npm test", tool_output_preview="42 passed", truncated=True)
        assert client.post("/v1/ingest/batches", headers={"authorization": f"Bearer {token}"},
                           json=batch(record)).status_code == 200
        event_id = f'{record["source_file_id"]}:12:0'
        path = f"/v1/events/{event_id}"
        response = client.get(path, headers={"authorization": "Bearer browser-token"})
        assert response.status_code == 200
        assert response.json()["tool_input"] == "npm test"
        assert response.json()["tool_output"] == "42 passed"
        assert response.json()["truncated"] is True
        assert response.headers["cache-control"] == "no-store"
        assert client.get(path, headers={"authorization": "Bearer other-browser-token"}).status_code == 404
        assert client.get(path, headers={"authorization": f"Bearer {token}"}).status_code == 401


def test_daily_provider_totals_delta_cumulative_usage_across_midnight() -> None:
    with TestClient(make_app()) as client:
        _, token = connect_device(client)
        records = []
        for i, (day, source, inp, out, cached, written, thinking, cumulative) in enumerate([
            ("2026-09-03", "codex", 100, 50, 80, 0, 20, True),
            ("2026-09-04", "codex", 140, 70, 110, 0, 30, True),
            ("2026-09-04", "codex", 140, 70, 110, 0, 30, True),
            ("2026-09-04", "claude-code", 10, 20, 100, 5, 8, False),
        ]):
            record = event_record()
            record.update(source=source, sequence=i)
            record["event"].update(type="usage", local_day=day, created_at=f"{day}T12:00:0{i}Z",
                token_input=inp, token_output=out, token_cache_read=cached, token_cache_write=written,
                token_thinking=thinking, usage_cumulative=cumulative)
            records.append(record)
        payload = batch(records[0]); payload["records"] = records
        assert client.post("/v1/ingest/batches", headers={"authorization": f"Bearer {token}"}, json=payload).status_code == 200
        data = client.get("/v1/day?date=2026-09-04", headers={"authorization": "Bearer browser-token"}).json()
        codex = data["tokens_by_source"]["2026-09-04"]["codex"]
        claude = data["tokens_by_source"]["2026-09-04"]["claude-code"]
        assert codex == {"in": 10, "out": 20, "cr": 30, "cw": 0, "th": 10, "total": 60}
        assert claude == {"in": 10, "out": 20, "cr": 100, "cw": 5, "th": 8, "total": 135}
        assert data["tokens"]["2026-09-04"]["in"] == 20
        assert "2026-09-03" not in data["tokens_by_source"]


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


def test_device_connection_is_visible_before_events_and_revoke_blocks_uploads() -> None:
    with TestClient(make_app()) as client:
        browser = {"authorization": "Bearer browser-token"}
        other = {"authorization": "Bearer other-browser-token"}
        assert client.get("/v1/devices", headers=browser).json() == []
        device_id, token = connect_device(client)
        response = client.get("/v1/devices", headers=browser)
        assert response.headers["cache-control"] == "no-store"
        device = response.json()[0]
        assert device["id"] == device_id
        assert device["status"] == "connected"
        assert device["name"] == "Test Mac"
        assert device["sessions"] == 0
        assert device["last_upload_at"] is None
        assert "token_hash" not in device and "device_token" not in device
        assert client.get("/v1/devices", headers=other).json() == []
        assert client.post(f"/v1/devices/{device_id}/revoke", headers=other).status_code == 404
        assert client.get("/v1/devices").status_code == 401
        device_auth = {"authorization": f"Bearer {token}"}
        assert client.get("/v1/devices", headers=device_auth).status_code == 401
        assert client.post(f"/v1/devices/{device_id}/revoke", headers=device_auth).status_code == 401
        payload = batch(event_record())
        assert client.post("/v1/ingest/batches", headers=device_auth, json=payload).status_code == 200
        device = client.get("/v1/devices", headers=browser).json()[0]
        assert device["sessions"] == 1 and device["last_upload_at"] is not None
        for _ in range(2):
            assert client.post(f"/v1/devices/{device_id}/revoke", headers=browser).status_code == 204
        assert client.get("/v1/devices", headers=browser).json()[0]["status"] == "revoked"
        assert client.post("/v1/ingest/batches", headers=device_auth, json=payload).status_code == 401
        assert client.get("/v1/dashboard?year=2026", headers=browser).json()["stats"]["strokes"] == 1


def test_calendar_and_day_are_separate_and_scoped() -> None:
    with TestClient(make_app()) as client:
        _, token = connect_device(client)
        client.post("/v1/ingest/batches", headers={"authorization": f"Bearer {token}"}, json=batch(event_record()))
        browser = {"authorization": "Bearer browser-token"}
        calendar = client.get("/v1/calendar?year=2026", headers=browser).json()
        assert calendar["rollups"]["2026-09-04"]["codex"]["tools"] == 1
        assert "events" not in calendar and "sessions" not in calendar
        detail = client.get("/v1/day?date=2026-09-04", headers=browser)
        assert detail.status_code == 200
        assert detail.json()["events"][0]["k"] == "tool"
        assert "rollups" not in detail.json()
        for path in ("/v1/calendar?year=2026", "/v1/day?date=2026-09-04"):
            assert client.get(path).status_code == 401
            assert client.get(path, headers={"authorization": f"Bearer {token}"}).status_code == 401
        assert client.get("/v1/calendar?year=2026", headers={"authorization": "Bearer other-browser-token"}).json()["rollups"] == {}
