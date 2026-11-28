"""The Telegram Mini App: a read-only page with the user's subscriptions.

`build_app` returns a Starlette app; `barabot.core.web` serves it under
PATH on the same public URL as the MCP connector, which it knows nothing
about. Inside the app:

    /                    the page, opened from the bot's menu button at `page_url`
    /api/subscriptions   JSON; Authorization: tma <initData>
    /api/photos/NAME     a chat photo the joiner downloaded; same header, and only
                         photos of chats in the user's own subscriptions

The page sends Telegram's initData with every request; `init_data.verify`
turns it into the user ID, so there are no sessions or tokens to store.
Adding and changing subscriptions stays in the chat with the bot.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, JSONResponse, Response
from starlette.routing import Route

from barabot.api import BarabotApi, SubscriptionRow
from barabot.core.db import connect
from barabot.domains.bot import reach
from barabot.domains.miniapp import init_data

PATH = "/app"


def page_url(public_url: str) -> str:
    """Where the bot's menu button points. The slash matters: the page fetches api/... relative to it."""
    return public_url + PATH + "/"


_PAGE = (Path(__file__).parent / "page.html").read_text(encoding="utf-8")
_PHOTO_NAME = re.compile(r"\d+\.jpg")


def _view(row: SubscriptionRow) -> dict[str, Any]:
    return {
        "id": row.id,
        "author": row.author,
        "author_not": row.author_not,
        "query": row.query,
        "chats": [
            {
                "chat": str(chat.ref),
                "title": chat.title,
                "username": chat.username,
                "photo": f"api/photos/{chat.photo}" if chat.photo else None,
                "status": chat.status.value,
                "folder": str(chat.folder) if chat.folder else None,
            }
            for chat in row.chats
        ],
    }


def _user_id(request: Request, bot_token: str) -> int:
    scheme, _, raw = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "tma" or not raw:
        raise init_data.InvalidInitData("open this page from the bot in Telegram")
    return init_data.verify(raw, bot_token)


def _owns_photo(db_path: Path, user_id: int, name: str) -> bool:
    conn = connect(db_path)
    try:
        row = conn.execute(
            "SELECT 1 FROM chats c JOIN subscription_chats sc ON sc.chat = c.id "
            "JOIN subscriptions s ON s.id = sc.subscription_id WHERE c.photo = ? AND s.user_id = ? LIMIT 1",
            (name, user_id),
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def build_app(
    api: BarabotApi,
    db_path: Path,
    photos_dir: Path,
    bot_token: str,
    bot_link: Callable[[], Awaitable[str]],
) -> Starlette:
    async def page(request: Request) -> Response:
        return HTMLResponse(_PAGE, headers={"Cache-Control": "no-cache"})

    async def subscriptions(request: Request) -> Response:
        try:
            user_id = _user_id(request, bot_token)
        except init_data.InvalidInitData as exc:
            return JSONResponse({"error": str(exc)}, status_code=401)
        rows = await api.list_subscriptions(user_id)
        blocked = await asyncio.to_thread(reach.unreachable_reason, db_path, user_id) is not None
        return JSONResponse(
            {
                "subscriptions": [_view(row) for row in rows],
                "notifications_blocked": blocked,
                "bot_link": await bot_link(),
            }
        )

    async def photo(request: Request) -> Response:
        try:
            user_id = _user_id(request, bot_token)
        except init_data.InvalidInitData as exc:
            return JSONResponse({"error": str(exc)}, status_code=401)
        name = request.path_params["name"]
        path = photos_dir / name
        if not _PHOTO_NAME.fullmatch(name) or not path.is_file():
            return JSONResponse({"error": "no such photo"}, status_code=404)
        if not await asyncio.to_thread(_owns_photo, db_path, user_id, name):
            return JSONResponse({"error": "no such photo"}, status_code=404)
        # A new photo gets a new name, so a cached one never goes stale.
        return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=604800"})

    return Starlette(
        routes=[Route("/", page), Route("/api/subscriptions", subscriptions), Route("/api/photos/{name}", photo)]
    )
