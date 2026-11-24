"""Telegram Bot API token verification and private persistence."""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote
from urllib.request import Request, urlopen


class AuthorizationError(RuntimeError):
    """A safe-to-display bot authorization failure."""


@dataclass(frozen=True)
class BotIdentity:
    id: int
    username: str


Verifier = Callable[[str], BotIdentity]


def verify_token(
    token: str,
    *,
    opener: Callable[..., Any] = urlopen,
) -> BotIdentity:
    """Verify a token using getMe, exposing no response or request secrets."""
    try:
        encoded_token = quote(token, safe=":")
        request = Request(
            f"https://api.telegram.org/bot{encoded_token}/getMe",
            headers={"Accept": "application/json"},
        )
        with opener(request, timeout=10.0) as response:
            payload = json.loads(response.read())

        if not isinstance(payload, dict) or payload.get("ok") is not True:
            raise ValueError("unsuccessful response")
        result = payload.get("result")
        if not isinstance(result, dict):
            raise ValueError("missing result")
        bot_id = result.get("id")
        username = result.get("username")
        if isinstance(bot_id, bool) or not isinstance(bot_id, int):
            raise ValueError("invalid bot id")
        if not isinstance(username, str) or not username:
            raise ValueError("invalid bot username")
        return BotIdentity(id=bot_id, username=username)
    except Exception:
        raise AuthorizationError("Telegram bot token verification failed") from None


def _atomic_write_token(token: str, token_path: Path) -> None:
    token_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    token_path.parent.chmod(0o700)
    fd, temporary_name = tempfile.mkstemp(dir=token_path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as temporary_file:
            temporary_file.write(token)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        os.replace(temporary_name, token_path)
        token_path.chmod(0o600)
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def authorize(
    token: str,
    token_path: Path,
    *,
    verifier: Verifier = verify_token,
) -> BotIdentity:
    """Verify first, then atomically replace the private token file."""
    try:
        identity = verifier(token)
    except Exception:
        raise AuthorizationError("Telegram bot token verification failed") from None
    _atomic_write_token(token, token_path)
    return identity
