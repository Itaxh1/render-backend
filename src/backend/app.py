from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, suppress
from datetime import date
from typing import AsyncIterator
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException, Query, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from psycopg import errors as pg_errors
from psycopg_pool import PoolTimeout, TooManyRequests

from .auth import SupabaseJWTVerifier, TokenVerifier, bearer_token
from .config import Settings, WorkerSettings
from .worker import run_embedded_worker
from .models import (
    BrowserDevice,
    CalendarPayload,
    DayPayload,
    EventDetail,
    ClaimResponse,
    DevicePrincipal,
    DeviceStatus,
    DashboardPayload,
    ExchangeClaimRequest,
    ExchangeClaimResponse,
    IngestBatch,
    IngestReceipt,
    PublicConfig,
    SummaryRequestResponse,
)
from .store import BatchConflictError, InvalidClaimError, InvalidDeviceError, Store
from .day_contract import (
    ChangedStoryCursor, DayExtrasResponse, DayRibbonResponse, DayStoryResponse,
    ExpiredStoryCursor, InvalidStoryCursor, MissingDaySession, day_axis,
)


def create_app(settings: Settings, store: Store, verifier: TokenVerifier | None = None) -> FastAPI:
    browser_verifier = verifier or SupabaseJWTVerifier(settings.supabase_url)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        await store.open()
        summary_task = None
        if settings.embedded_summaries:
            summary_task = asyncio.create_task(run_embedded_worker(WorkerSettings(
                database_url=settings.database_url,
                xai_api_key=settings.xai_api_key or "",
                xai_model=settings.xai_model,
            )), name="summary-worker")
        try:
            yield
        finally:
            if summary_task is not None:
                summary_task.cancel()
                with suppress(asyncio.CancelledError):
                    await summary_task
            await store.close()

    app = FastAPI(title="Rexy API", version="0.1.0", lifespan=lifespan)

    async def database_busy(_request, _error):
        return JSONResponse(status_code=503, content={'detail':'API temporarily unavailable'},
                            headers={'Cache-Control':'no-store','Retry-After':'2'})

    for error in (PoolTimeout, TooManyRequests, pg_errors.QueryCanceled, pg_errors.LockNotAvailable,
                  pg_errors.DeadlockDetected, pg_errors.SerializationFailure):
        app.add_exception_handler(error, database_busy)

    async def storage_unavailable(_request, _error):
        return JSONResponse(status_code=503, content={'detail':
            'Database storage is unavailable. Uploads are paused; saved history is retained.'},
            headers={'Cache-Control':'no-store','Retry-After':'60'})

    for error in (pg_errors.ReadOnlySqlTransaction, pg_errors.DiskFull):
        app.add_exception_handler(error, storage_unavailable)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[settings.rexy_web_origin],
        allow_credentials=True,
        allow_methods=["GET", "POST"],
        allow_headers=["authorization", "content-type"],
    )

    @app.middleware("http")
    async def reject_oversized_ingest(request: Request, call_next):
        if request.url.path == "/v1/ingest/batches":
            content_length = request.headers.get("content-length")
            if content_length:
                try:
                    too_large = int(content_length) > 4 * 1024 * 1024
                except ValueError:
                    return JSONResponse(
                        status_code=400, content={"detail": "invalid content length"}
                    )
                if too_large:
                    return JSONResponse(
                        status_code=413, content={"detail": "request too large"}
                    )
        return await call_next(request)

    async def browser_user(request: Request):
        return await browser_verifier.verify(bearer_token(request))

    async def device(request: Request) -> DevicePrincipal:
        principal = await store.authenticate_device(bearer_token(request))
        if principal is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="invalid device token",
                headers={"WWW-Authenticate": "Bearer"},
            )
        return principal

    @app.get("/healthz")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    async def readiness() -> dict[str, str]:
        if not await store.ready():
            raise HTTPException(status_code=503, detail="database unavailable")
        return {"status": "ready"}

    @app.get("/v1/public/config", response_model=PublicConfig)
    async def public_config() -> PublicConfig:
        if not settings.supabase_publishable_key:
            raise HTTPException(status_code=503, detail="browser authentication is not configured")
        return PublicConfig(
            supabase_url=settings.supabase_url,
            supabase_publishable_key=settings.supabase_publishable_key,
        )

    @app.post("/v1/install/claims", response_model=ClaimResponse, status_code=201)
    async def create_claim(user_id=Depends(browser_user)) -> ClaimResponse:
        token, expires_at = await store.create_claim(user_id)
        return ClaimResponse(claim_token=token, expires_at=expires_at)

    @app.post(
        "/v1/devices/exchange-claim",
        response_model=ExchangeClaimResponse,
        status_code=201,
    )
    async def exchange_claim(request: ExchangeClaimRequest) -> ExchangeClaimResponse:
        try:
            device_id, token = await store.exchange_claim(
                request.claim_token, request.device_name, request.platform
            )
        except InvalidClaimError as error:
            raise HTTPException(status_code=400, detail="invalid or expired claim") from error
        return ExchangeClaimResponse(device_id=device_id, device_token=token)

    @app.get("/v1/devices", response_model=list[BrowserDevice])
    async def list_devices(response: Response, user_id=Depends(browser_user)):
        response.headers["Cache-Control"] = "no-store"
        return await store.list_devices(user_id)

    @app.post("/v1/devices/{device_id}/revoke", status_code=204)
    async def revoke_device(device_id: UUID, user_id=Depends(browser_user)):
        if not await store.revoke_device(user_id, device_id):
            raise HTTPException(status_code=404, detail="device not found")
        return Response(status_code=204, headers={"Cache-Control": "no-store"})

    @app.post("/v1/ingest/batches", response_model=IngestReceipt)
    async def ingest_batch(
        batch: IngestBatch,
        principal: DevicePrincipal = Depends(device),
    ) -> IngestReceipt:
        try:
            return await store.ingest(principal, batch)
        except BatchConflictError as error:
            raise HTTPException(
                status_code=409, detail="batch id or sequence was reused with different content"
            ) from error
        except InvalidDeviceError as error:
            raise HTTPException(status_code=401, detail="invalid device token") from error

    @app.get("/v1/ingest/status", response_model=DeviceStatus)
    async def ingest_status(
        principal: DevicePrincipal = Depends(device),
    ) -> DeviceStatus:
        return DeviceStatus(device_id=principal.device_id)

    @app.get("/v1/calendar", response_model=CalendarPayload)
    async def calendar(response: Response, year: int = Query(ge=2020, le=2100), user_id=Depends(browser_user)):
        response.headers["Cache-Control"] = "no-store"
        return await store.calendar(user_id, year)

    @app.get("/v1/day", response_model=DayPayload)
    async def day_detail(response: Response, date: date = Query(), user_id=Depends(browser_user)):
        response.headers["Cache-Control"] = "no-store"
        return await store.day_detail(user_id, date)

    @app.get("/v1/day/ribbon", response_model=DayRibbonResponse)
    async def day_ribbon(response: Response, date: date = Query(), tz: str = Query(default='UTC', max_length=128),
                         user_id=Depends(browser_user)):
        response.headers['Cache-Control'] = 'no-store'
        try:
            day_axis(date, tz)
        except ValueError as error:
            raise HTTPException(status_code=422, detail='Invalid IANA timezone') from error
        return await store.day_ribbon(user_id, date, tz)

    @app.get("/v1/day/extras", response_model=DayExtrasResponse)
    async def day_extras(response: Response, date: date = Query(), user_id=Depends(browser_user)):
        response.headers['Cache-Control'] = 'no-store'
        return await store.day_extras(user_id, date)

    @app.get("/v1/day/story", response_model=DayStoryResponse)
    async def day_story(response: Response, date: date = Query(),
                        session_id: int | None = Query(default=None, ge=1, le=9223372036854775807),
                        cursor: str | None = Query(default=None, max_length=4096),
                        limit: int = Query(default=200, ge=1, le=500), user_id=Depends(browser_user)):
        response.headers['Cache-Control'] = 'no-store'
        try:
            return await store.day_story(user_id, date, str(session_id) if session_id is not None else None, cursor, limit)
        except ExpiredStoryCursor as error:
            raise HTTPException(status_code=410, detail=str(error)) from error
        except ChangedStoryCursor as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except InvalidStoryCursor as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        except MissingDaySession as error:
            raise HTTPException(status_code=404, detail=str(error)) from error

    @app.get("/v1/events/{event_id}", response_model=EventDetail)
    async def event_detail(event_id: str, response: Response, user_id=Depends(browser_user)):
        response.headers["Cache-Control"] = "no-store"
        detail = await store.event_detail(user_id, event_id)
        if detail is None:
            raise HTTPException(status_code=404, detail="event not found")
        return detail

    @app.get("/v1/dashboard", response_model=DashboardPayload)
    async def dashboard(
        year: int = Query(ge=2020, le=2100),
        day: date | None = Query(default=None),
        user_id=Depends(browser_user),
    ) -> DashboardPayload:
        if day is not None and day.year != year:
            raise HTTPException(status_code=422, detail="day must fall within year")
        return await store.dashboard(user_id, year, day)

    @app.post(
        "/v1/sessions/{session_id}/summaries",
        response_model=SummaryRequestResponse,
        status_code=202,
    )
    async def request_summary(
        session_id: int,
        user_id=Depends(browser_user),
    ) -> SummaryRequestResponse:
        queued = await store.request_summary(user_id, session_id)
        if not queued:
            raise HTTPException(status_code=404, detail="session not found")
        return SummaryRequestResponse(session_id=str(session_id), state="pending")

    return app
