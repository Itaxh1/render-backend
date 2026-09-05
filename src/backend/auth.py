from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol
from uuid import UUID

import httpx
from fastapi import HTTPException, Request, status
from jose import JWTError, jwt


class TokenVerifier(Protocol):
    async def verify(self, token: str) -> UUID: ...


def bearer_token(request: Request) -> str:
    value = request.headers.get("authorization", "")
    scheme, separator, token = value.partition(" ")
    if separator != " " or scheme.lower() != "bearer" or not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return token


class SupabaseJWTVerifier:
    def __init__(self, supabase_url: str, *, cache_seconds: int = 600) -> None:
        self._issuer = f"{supabase_url.rstrip('/')}/auth/v1"
        self._jwks_url = f"{self._issuer}/.well-known/jwks.json"
        self._cache_seconds = cache_seconds
        self._keys: dict[str, Any] | None = None
        self._expires_at = datetime.min.replace(tzinfo=timezone.utc)
        self._lock = asyncio.Lock()

    async def _jwks(self) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        if self._keys is not None and now < self._expires_at:
            return self._keys
        async with self._lock:
            now = datetime.now(timezone.utc)
            if self._keys is not None and now < self._expires_at:
                return self._keys
            async with httpx.AsyncClient(timeout=5.0) as client:
                response = await client.get(self._jwks_url)
                response.raise_for_status()
                keys = response.json()
            if not isinstance(keys, dict) or not keys.get("keys"):
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="Supabase asymmetric signing keys are unavailable",
                )
            self._keys = keys
            self._expires_at = now + timedelta(seconds=self._cache_seconds)
            return keys

    async def verify(self, token: str) -> UUID:
        try:
            header = jwt.get_unverified_header(token)
            algorithm = header.get("alg")
            if algorithm not in {"RS256", "ES256"}:
                raise JWTError("unsupported signing algorithm")
            payload = jwt.decode(
                token,
                await self._jwks(),
                algorithms=[algorithm],
                issuer=self._issuer,
                audience="authenticated",
                options={"require_sub": True, "require_exp": True},
            )
            return UUID(payload["sub"])
        except HTTPException:
            raise
        except (JWTError, KeyError, TypeError, ValueError) as error:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="invalid access token",
                headers={"WWW-Authenticate": "Bearer"},
            ) from error
