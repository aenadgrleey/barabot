"""The MCP connector: a remote MCP server over `BarabotApi`, with login through Telegram.

A user adds `<PUBLIC_URL>/mcp` as a connector in Claude (or any MCP
client with OAuth), logs in with Telegram once and then manages
subscriptions by talking to their own agent. Notifications still arrive
from the bot; the login's `telegram:bot_access` scope lets the bot write
first, so /start is not needed. The login only completes once the bot's
welcome message is delivered: without it barabot cannot notify anyone.

    /mcp                      streamable HTTP, bearer token required
    /.well-known/...          OAuth metadata, served by the MCP SDK
    /register /authorize /token /revoke   OAuth, see `oauth.py`
    /telegram/callback        Telegram's redirect after login

`build_connector` returns the Starlette app; `barabot.core.web` serves it
at the root of PUBLIC_URL, next to other domains' apps.

Tool descriptions are the product here: the user's agent writes the
subscription text, and Jev's results depend on how it is worded.
"""

from __future__ import annotations

import asyncio
import contextlib
import html
import logging
from collections.abc import Awaitable, Callable, Iterator
from pathlib import Path
from typing import Annotated, Any, Protocol
from urllib.parse import urlsplit

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from pydantic import Field
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

from barabot.api import UNSET, ApiError, BarabotApi, SubscriptionRow
from barabot.core.config import Config
from barabot.domains.bot import reach
from barabot.domains.bot.notifier import BotSender
from barabot.domains.mcp.oauth import BarabotOAuthProvider, LoginNotFound
from barabot.domains.mcp.telegram_login import TelegramLogin

log = logging.getLogger(__name__)

CALLBACK_PATH = "/telegram/callback"

WELCOME = (
    "Barabot is connected to your assistant. Ask it to set up a subscription; "
    "matching listings will arrive in this chat."
)

BOT_BLOCKED = (
    "VERY IMPORTANT: barabot cannot message this user in Telegram (they blocked the bot or never "
    "started it). Notifications are the whole point of barabot: without them no subscription does "
    "anything, so new or changed subscriptions are refused. Tell the user this before anything else "
    "and ask them to open {link}, press Start (or Unblock), and then try again."
)

BOT_UNREACHABLE_PAGE = """<!doctype html><meta charset="utf-8">
<title>Barabot cannot message you</title>
<h1>Very important: let the bot message you</h1>
<p>Barabot sends matching listings as Telegram messages. Without them it does nothing for you,
so you cannot connect until the bot can message you.</p>
<p>Open <a href="{link}">the bot</a>, press Start, then connect again from your assistant.</p>
"""


class ConnectorBot(BotSender, Protocol):
    async def link(self) -> str:
        """https://t.me/<bot username>."""
        ...


def login_bot_mismatch(token_path: Path, login_client_id: str) -> str | None:
    """Why the Telegram login belongs to another bot, or None when it is ours.

    telegram:bot_access lets the login's bot write to the user, so it must be the bot
    in config/bot.token. A missing token file is left to setup.
    """
    if not token_path.exists():
        return None
    bot_id = token_path.read_text(encoding="utf-8").strip().split(":", 1)[0]
    if bot_id == login_client_id:
        return None
    return (
        f"TELEGRAM_LOGIN_CLIENT_ID is bot {login_client_id}, but config/bot.token is bot {bot_id}. "
        "Set up the Login Widget on the bot itself in @BotFather and use its client ID and secret."
    )


INSTRUCTIONS = """\
Barabot watches Telegram chats (flea markets, rentals, job boards) and sends the user a bot \
message for every new post that matches one of their subscriptions. You manage those subscriptions.

A subscription has three texts, judged separately by a classifier for every post:
- author: what the author of a wanted post does, as a role. "сдаёт квартиру", "продаёт велосипед".
- author_not: the mirror role that must NOT match. "ищет квартиру для аренды", "ищет или покупает велосипед".
  Always write it when the topic has two sides (rent out / look for, sell / buy, offer / seek work).
  Without it the classifier confuses the sides, because they talk about the same thing.
- query: what the post must be about, with the user's constraints. "квартира от 2 комнат, до 20 000 Kč в месяц".

Write the texts in the language of the chats (usually Russian), short and literal, one role per field. \
Put conditions on the item into query, never into author. Ask the user when the side (offering or seeking) \
is unclear. A subscription only sees the chats it lists; take them as @username or t.me links from the user. \
New subscriptions only see posts that arrive after they are saved.

VERY IMPORTANT: notifications are the whole product. If a tool result says the bot cannot message \
the user (an error, or notifications_blocked in list_subscriptions), tell the user right away and \
send them to the bot to press Start or Unblock. Do not continue as if their subscriptions work.\
"""

NEW_SUBSCRIPTION_PROMPT = """\
Help me set up a Barabot subscription for this: {request}

Before saving, work out with me:
1. Which side I am on: do I want posts from people who offer the thing, or from people who seek it?
2. author: the role of the post author I want, in a few words.
3. author_not: the opposite role in the same topic, so the other side is filtered out.
4. query: the item and my constraints (size, price, area, condition).
5. chats: which Telegram chats to watch, as @username or t.me links.

Show me the three texts and the chats, then call add_subscription when I agree.\
"""


def _user_id() -> int:
    token = get_access_token()
    if token is None or token.subject is None:
        raise ToolError("not logged in")
    return int(token.subject)


@contextlib.contextmanager
def _api_errors() -> Iterator[None]:
    try:
        yield
    except ApiError as exc:
        raise ToolError(str(exc)) from exc


def subscription_view(row: SubscriptionRow) -> dict[str, Any]:
    return {
        "id": row.id,
        "author": row.author,
        "author_not": row.author_not,
        "query": row.query,
        "chats": [
            {
                "chat": str(chat.ref),
                "title": chat.title,
                "status": chat.status.value,
                "folder": str(chat.folder) if chat.folder else None,
            }
            for chat in row.chats
        ],
    }


def build_server(
    api: BarabotApi,
    provider: BarabotOAuthProvider,
    public_url: str,
    bot_blocked: Callable[[int], Awaitable[str | None]],
) -> MCPServer:
    """`bot_blocked(user_id)` says why the bot cannot write to the user, or None when it can."""

    async def require_bot(user_id: int) -> None:
        blocked = await bot_blocked(user_id)
        if blocked:
            raise ToolError(blocked)

    server = MCPServer(
        name="barabot",
        title="Barabot",
        instructions=INSTRUCTIONS,
        auth_server_provider=provider,
        auth=AuthSettings(
            issuer_url=public_url,  # pyright: ignore[reportArgumentType]  # pydantic parses the str
            resource_server_url=f"{public_url}/mcp",  # pyright: ignore[reportArgumentType]
            # One resource, one authorization server: every token we issue is for /mcp.
            validate_token_resource=False,
            client_registration_options=ClientRegistrationOptions(enabled=True),
            revocation_options=RevocationOptions(enabled=True),
        ),
    )

    @server.tool(annotations=ToolAnnotations(read_only_hint=True))
    async def list_subscriptions() -> dict[str, Any]:
        """List the user's subscriptions with the status of each chat.

        Chat status: pending (barabot has not joined yet), joined (watching),
        requested (waiting for a chat admin to approve), failed (no such chat or joining failed).
        """
        user_id = _user_id()
        rows = await api.list_subscriptions(user_id)
        result: dict[str, Any] = {"subscriptions": [subscription_view(row) for row in rows]}
        blocked = await bot_blocked(user_id)
        if blocked:
            result["notifications_blocked"] = blocked
        return result

    @server.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False))
    async def add_subscription(
        author: Annotated[str, Field(description="Role of the wanted post's author, e.g. 'сдаёт квартиру'")],
        query: Annotated[str, Field(description="What the post must be about, with the user's constraints")],
        chats: Annotated[list[str], Field(description="Chats to watch: @username, t.me links or -100... IDs")],
        author_not: Annotated[
            str | None, Field(description="Mirror role that must not match, e.g. 'ищет квартиру'")
        ] = None,
    ) -> dict[str, Any]:
        """Create a subscription. Confirm the texts with the user first; see the server instructions."""
        await require_bot(_user_id())
        with _api_errors():
            row = await api.add_subscription(_user_id(), author=author, query=query, chats=chats, author_not=author_not)
        return subscription_view(row)

    @server.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=True))
    async def update_subscription(
        subscription_id: int,
        author: str | None = None,
        query: str | None = None,
        author_not: Annotated[
            str | None, Field(description="New mirror role; an empty string removes it; omit to keep")
        ] = None,
        chats: Annotated[list[str] | None, Field(description="Replaces the whole chat list; omit to keep")] = None,
    ) -> dict[str, Any]:
        """Change some fields of a subscription; omitted fields stay as they are."""
        await require_bot(_user_id())
        with _api_errors():
            row = await api.update_subscription(
                _user_id(),
                subscription_id,
                author=UNSET if author is None else author,
                query=UNSET if query is None else query,
                author_not=UNSET if author_not is None else author_not,
                chats=UNSET if chats is None else chats,
            )
        return subscription_view(row)

    @server.tool(annotations=ToolAnnotations(destructive_hint=True))
    async def remove_subscription(subscription_id: int) -> str:
        """Delete a subscription. The user stops getting its notifications."""
        with _api_errors():
            await api.remove_subscription(_user_id(), subscription_id)
        return f"subscription {subscription_id} removed"

    @server.prompt(title="New subscription")
    def new_subscription(request: str) -> str:
        """Walk through a new subscription: side, roles, query and chats."""
        return NEW_SUBSCRIPTION_PROMPT.format(request=request)

    return server


def add_login_callback(server: MCPServer, provider: BarabotOAuthProvider, bot: ConnectorBot) -> None:
    @server.custom_route(CALLBACK_PATH, methods=["GET"])
    async def telegram_callback(request: Request) -> Response:
        params = request.query_params
        try:
            result = await provider.finish_login(params.get("state", ""), params.get("code"), params.get("error"))
        except LoginNotFound:
            return HTMLResponse(
                "<p>This login link has expired. Start connecting again from your assistant.</p>", status_code=400
            )
        if result.bot_unreachable:
            link = await bot.link()
            return HTMLResponse(BOT_UNREACHABLE_PAGE.format(link=html.escape(link)), status_code=403)
        return RedirectResponse(result.redirect_url, status_code=302)


def build_app(db_path: Path, login: TelegramLogin, bot: ConnectorBot, public_url: str, *, port: int) -> Starlette:
    """The connector's app: OAuth endpoints, the Telegram callback and /mcp."""

    async def greet(user_id: int) -> None:
        await bot.send(user_id, WELCOME)
        await asyncio.to_thread(reach.mark_reachable, db_path, user_id)

    async def bot_blocked(user_id: int) -> str | None:
        if await asyncio.to_thread(reach.unreachable_reason, db_path, user_id) is None:
            return None
        return BOT_BLOCKED.format(link=await bot.link())

    provider = BarabotOAuthProvider(db_path, login, greet)
    server = build_server(BarabotApi(db_path), provider, public_url, bot_blocked)
    add_login_callback(server, provider, bot)
    return server.streamable_http_app(
        stateless_http=True,  # no session state to lose on restart
        json_response=True,
        transport_security=TransportSecuritySettings(
            allowed_hosts=[urlsplit(public_url).netloc, f"127.0.0.1:{port}", f"localhost:{port}"],
            allowed_origins=[public_url, "https://claude.ai"],
        ),
    )


def build_connector(cfg: Config, db_path: Path, bot: ConnectorBot) -> Starlette:
    """The connector's app from the configuration. The login must belong to the bot itself."""
    mismatch = login_bot_mismatch(Config.bot_token_path, cfg.mcp.login_client_id)
    if mismatch:
        raise RuntimeError(mismatch)
    login = TelegramLogin(cfg.mcp.login_client_id, cfg.mcp.login_client_secret, cfg.web.public_url + CALLBACK_PATH)
    return build_app(db_path, login, bot, cfg.web.public_url, port=cfg.web.port)
