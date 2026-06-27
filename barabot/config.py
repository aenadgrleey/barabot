"""Config loading. Reads .env from the project root (one above this package)."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")

_VALID_LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}


def _required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(
            f"{name} is missing. Copy .env.example to .env and fill it in."
        )
    return value


def _int(name: str, default: int) -> int:
    raw = os.getenv(name)
    return int(raw) if raw else default


def _float(name: str, default: float) -> float:
    raw = os.getenv(name)
    return float(raw) if raw else default


def _list(name: str) -> list[int]:
    raw = os.getenv(name, "").strip()
    if not raw:
        return []
    return [int(x.strip()) for x in raw.split(",") if x.strip()]


@dataclass(frozen=True)
class Config:
    api_id: int
    api_hash: str
    session_string: str
    openai_api_key: str
    embedding_model: str
    embedding_url: str
    similarity_threshold: float
    watch_chats: list[int]
    log_level: str
    embedding_concurrency: int
    embedding_retries: int
    embedding_timeout: int

    db_path: Path = field(default_factory=lambda: PROJECT_ROOT / "data" / "barabot.sqlite")
    lock_path: Path = field(default_factory=lambda: PROJECT_ROOT / "data" / "barabot.lock")
    pid_path: Path = field(default_factory=lambda: PROJECT_ROOT / "data" / "barabot.pid")


def _validate_api_hash(value: str) -> None:
    if not re.fullmatch(r"[0-9a-fA-F]{32}", value):
        raise RuntimeError(
            "API_HASH must be 32 hex characters (get it from https://my.telegram.org)"
        )


def _validate_threshold(value: float) -> None:
    if not 0.0 <= value <= 1.0:
        raise RuntimeError(f"SIMILARITY_THRESHOLD must be in [0, 1], got {value}")


def load() -> Config:
    api_hash = _required("API_HASH")
    _validate_api_hash(api_hash)
    threshold = _float("SIMILARITY_THRESHOLD", 0.85)
    _validate_threshold(threshold)
    log_level = os.getenv("LOG_LEVEL", "INFO").upper()
    if log_level not in _VALID_LOG_LEVELS:
        raise RuntimeError(
            f"LOG_LEVEL must be one of {sorted(_VALID_LOG_LEVELS)}, got {log_level!r}"
        )
    concurrency = _int("EMBEDDING_CONCURRENCY", 4)
    if concurrency < 1:
        raise RuntimeError("EMBEDDING_CONCURRENCY must be >= 1")
    retries = _int("EMBEDDING_RETRIES", 3)
    if retries < 0:
        raise RuntimeError("EMBEDDING_RETRIES must be >= 0")
    timeout = _int("EMBEDDING_TIMEOUT", 30)
    if timeout < 1:
        raise RuntimeError("EMBEDDING_TIMEOUT must be >= 1")

    return Config(
        api_id=int(_required("API_ID")),
        api_hash=api_hash,
        session_string=_required("SESSION_STRING"),
        openai_api_key=_required("OPENAI_API_KEY"),
        embedding_model=os.getenv("EMBEDDING_MODEL", "text-embedding-3-small"),
        embedding_url=os.getenv("EMBEDDING_URL", "https://api.openai.com/v1/embeddings"),
        similarity_threshold=threshold,
        watch_chats=_list("WATCH_CHATS"),
        log_level=log_level,
        embedding_concurrency=concurrency,
        embedding_retries=retries,
        embedding_timeout=timeout,
    )
