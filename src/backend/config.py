from __future__ import annotations

import os
from dataclasses import dataclass
from urllib.parse import urlparse


@dataclass(frozen=True)
class Settings:
    database_url: str
    supabase_url: str
    rexy_web_origin: str
    xai_api_key: str | None = None
    supabase_publishable_key: str | None = None
    xai_model: str = "grok-4.3"
    embedded_summaries: bool = False

    @classmethod
    def from_environment(cls) -> "Settings":
        database_url = os.environ.get("DATABASE_URL", "")
        supabase_url = os.environ.get("SUPABASE_URL", "")
        web_origin = os.environ.get("REXY_WEB_ORIGIN", "http://localhost:5173")
        if not database_url:
            raise RuntimeError("DATABASE_URL is required")
        _require_http_url("SUPABASE_URL", supabase_url, https_only=True)
        _require_http_url("REXY_WEB_ORIGIN", web_origin, https_only=False)
        embedded_summaries = os.environ.get("REXY_EMBED_SUMMARIES", "0") == "1"
        if embedded_summaries and not os.environ.get("XAI_API_KEY"):
            raise RuntimeError("XAI_API_KEY is required when REXY_EMBED_SUMMARIES=1")
        return cls(
            database_url=database_url,
            supabase_url=supabase_url.rstrip("/"),
            rexy_web_origin=web_origin.rstrip("/"),
            xai_api_key=os.environ.get("XAI_API_KEY") or None,
            supabase_publishable_key=os.environ.get("SUPABASE_PUBLISHABLE_KEY") or None,
            xai_model=os.environ.get("XAI_MODEL", "grok-4.3"),
            embedded_summaries=embedded_summaries,
        )


@dataclass(frozen=True)
class WorkerSettings:
    database_url: str
    xai_api_key: str
    xai_model: str = "grok-4.3"

    @classmethod
    def from_environment(cls) -> "WorkerSettings":
        database_url = os.environ.get("DATABASE_URL", "")
        xai_api_key = os.environ.get("XAI_API_KEY", "")
        if not database_url:
            raise RuntimeError("DATABASE_URL is required")
        if not xai_api_key:
            raise RuntimeError("XAI_API_KEY is required")
        return cls(
            database_url=database_url,
            xai_api_key=xai_api_key,
            xai_model=os.environ.get("XAI_MODEL", "grok-4.3"),
        )


def _require_http_url(name: str, value: str, *, https_only: bool) -> None:
    parsed = urlparse(value)
    permitted = {"https"} if https_only else {"http", "https"}
    if parsed.scheme not in permitted or not parsed.netloc:
        protocols = "HTTPS" if https_only else "HTTP(S)"
        raise RuntimeError(f"{name} must be a valid {protocols} URL")
