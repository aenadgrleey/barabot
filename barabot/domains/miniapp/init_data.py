"""Who opened the Mini App: Telegram's `initData`, checked with the bot token.

Telegram hands the page a query string with the user and a `hash`, an
HMAC-SHA256 of the other fields under a key derived from the bot token
(https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app).
Only Telegram and barabot know the token, so a valid hash proves the user.
`auth_date` limits how long a leaked string works.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from urllib.parse import parse_qsl

MAX_AGE_SECONDS = 24 * 60 * 60  # a Mini App left open keeps the initData it was opened with


class InvalidInitData(Exception):
    pass


def sign(fields: dict[str, str], bot_token: str) -> str:
    """The `hash` Telegram puts next to these fields."""
    check_string = "\n".join(f"{key}={fields[key]}" for key in sorted(fields))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    return hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()


def verify(raw: str, bot_token: str, *, now: float | None = None) -> int:
    """The Telegram user ID from a valid initData string; `InvalidInitData` otherwise."""
    try:
        fields = dict(parse_qsl(raw, keep_blank_values=True, strict_parsing=True))
    except ValueError as exc:
        raise InvalidInitData("malformed initData") from exc
    received = fields.pop("hash", "")
    if not received or not hmac.compare_digest(received, sign(fields, bot_token)):
        raise InvalidInitData("initData is not signed by this bot")
    try:
        auth_date = int(fields["auth_date"])
        user_id = json.loads(fields["user"])["id"]
    except (KeyError, ValueError, TypeError) as exc:
        raise InvalidInitData("initData has no user or auth_date") from exc
    if (time.time() if now is None else now) - auth_date > MAX_AGE_SECONDS:
        raise InvalidInitData("initData has expired; reopen the Mini App")
    if not isinstance(user_id, int):
        raise InvalidInitData("initData user has no numeric id")
    return user_id
