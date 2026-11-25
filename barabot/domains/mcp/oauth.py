"""OAuth for the MCP connector: barabot is the authorization server, Telegram says who the user is.

MCP clients such as Claude register themselves (dynamic client
registration), send the user to our `/authorize` and trade the code for
our access and refresh tokens. The MCP SDK serves those endpoints and
checks PKCE; this provider stores the state in SQLite and plugs Telegram
into the middle:

    client  -> /authorize           authorize(): remember the request, go to Telegram
    browser -> oauth.telegram.org   the user confirms the login in Telegram
    browser -> /telegram/callback   finish_login(): verify the ID token, issue our code
    client  -> /token               exchange_authorization_code(): issue our tokens

A token's `subject` is the Telegram user ID as a string; tools pass it to
`BarabotApi`. Codes and tokens are random and stored as SHA-256 hashes.
Refresh rotates the whole grant. Blocking SQLite runs in a thread.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import secrets
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    OAuthAuthorizationServerProvider,
    RefreshToken,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from barabot.core.db import connect, transaction
from barabot.domains.mcp.telegram_login import TelegramLogin, TelegramLoginError, TelegramUser

log = logging.getLogger(__name__)

LOGIN_TTL = 600  # the user has ten minutes to confirm in Telegram
CODE_TTL = 300
ACCESS_TTL = 3600
REFRESH_TTL = 90 * 24 * 3600


class LoginNotFound(Exception):
    """The callback's state is unknown or expired: nowhere to send the browser back."""


@dataclass(frozen=True)
class LoginResult:
    redirect_url: str  # back to the MCP client, with a code or an error
    user: TelegramUser | None  # None when the login failed
    bot_unreachable: bool = False  # Telegram said who the user is, but the bot cannot write to them


def _hash(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


class BarabotOAuthProvider(OAuthAuthorizationServerProvider[AuthorizationCode, RefreshToken, AccessToken]):
    """`OAuthAuthorizationServerProvider` over `data/barabot.sqlite`."""

    def __init__(self, db_path: Path, login: TelegramLogin, greet: Callable[[int], Awaitable[object]]) -> None:
        self._db_path = db_path
        self._login = login
        # Sends the welcome message. Notifications are the product, so a login whose
        # user the bot cannot write to gets no code.
        self._greet = greet

    # Clients

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        row = await self._one("SELECT info FROM oauth_clients WHERE client_id = ?", (client_id,))
        return OAuthClientInformationFull.model_validate_json(row[0]) if row else None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        assert client_info.client_id is not None, "the SDK assigns client_id before saving"
        await self._write(
            "INSERT OR REPLACE INTO oauth_clients (client_id, info) VALUES (?, ?)",
            (client_info.client_id, client_info.model_dump_json()),
        )

    # Authorization

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        state = secrets.token_urlsafe(32)
        start = self._login.start(state)
        await asyncio.to_thread(self._save_login, state, client.client_id or "", params, start.verifier, start.nonce)
        return start.url

    def _save_login(self, state: str, client_id: str, params: AuthorizationParams, verifier: str, nonce: str) -> None:
        now = time.time()
        conn = connect(self._db_path)
        try:
            with transaction(conn):
                # Housekeeping rides on new logins: they are rare and cheap.
                conn.execute("DELETE FROM oauth_logins WHERE expires_at < ?", (now,))
                conn.execute("DELETE FROM oauth_codes WHERE expires_at < ?", (now,))
                conn.execute("DELETE FROM oauth_tokens WHERE expires_at < ?", (now,))
                conn.execute(
                    "INSERT INTO oauth_logins (state, client_id, request, verifier, nonce, expires_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (state, client_id, params.model_dump_json(), verifier, nonce, now + LOGIN_TTL),
                )
        finally:
            conn.close()

    async def finish_login(self, state: str, code: str | None, error: str | None) -> LoginResult:
        """Handle Telegram's redirect to our callback.

        Raises LoginNotFound when the state is unknown or expired; otherwise
        returns where to send the browser.
        """
        row = await self._one(
            "DELETE FROM oauth_logins WHERE state = ? AND expires_at >= ? RETURNING client_id, request, verifier, nonce",
            (state, time.time()),
            write=True,
        )
        if row is None:
            raise LoginNotFound(state)
        client_id, request, verifier, nonce = row
        params = AuthorizationParams.model_validate_json(request)
        back = str(params.redirect_uri)

        if error or not code:
            log.info("Telegram login declined: %s", error or "no code")
            return LoginResult(construct_redirect_uri(back, error="access_denied", state=params.state), None)
        try:
            user = await asyncio.to_thread(self._login.exchange_code, code, verifier=verifier, nonce=nonce)
        except TelegramLoginError as exc:
            log.warning("%s", exc)
            return LoginResult(
                construct_redirect_uri(
                    back, error="access_denied", error_description="Telegram login failed", state=params.state
                ),
                None,
            )

        try:
            await self._greet(user.id)
        except Exception as exc:
            log.warning("Bot cannot write to Telegram user %s: %s", user.id, exc)
            return LoginResult(
                construct_redirect_uri(
                    back, error="access_denied", error_description="The bot cannot message you", state=params.state
                ),
                user,
                bot_unreachable=True,
            )

        our_code = secrets.token_urlsafe(32)
        await self._write(
            "INSERT INTO oauth_codes (code_hash, client_id, user_id, request, expires_at) VALUES (?, ?, ?, ?, ?)",
            (_hash(our_code), client_id, user.id, request, time.time() + CODE_TTL),
        )
        log.info("Telegram user %s connected an MCP client", user.id)
        return LoginResult(construct_redirect_uri(back, code=our_code, state=params.state), user)

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        row = await self._one(
            "SELECT user_id, request, expires_at FROM oauth_codes WHERE code_hash = ? AND client_id = ?",
            (_hash(authorization_code), client.client_id),
        )
        if row is None:
            return None
        user_id, request, expires_at = row
        params = AuthorizationParams.model_validate_json(request)
        return AuthorizationCode(
            code=authorization_code,
            scopes=params.scopes or [],
            expires_at=expires_at,
            client_id=client.client_id or "",
            code_challenge=params.code_challenge,
            redirect_uri=params.redirect_uri,
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            resource=params.resource,
            subject=str(user_id),
        )

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        # Deleting first makes a code single-use even when two requests race.
        used = await self._one(
            "DELETE FROM oauth_codes WHERE code_hash = ? RETURNING user_id",
            (_hash(authorization_code.code),),
            write=True,
        )
        if used is None:
            raise TokenError("invalid_grant", "authorization code already used")
        return await self._issue(
            client.client_id or "", int(used[0]), authorization_code.scopes, authorization_code.resource
        )

    # Tokens

    async def load_refresh_token(self, client: OAuthClientInformationFull, refresh_token: str) -> RefreshToken | None:
        row = await self._token(refresh_token, "refresh")
        if row is None or row.client_id != client.client_id:
            return None
        return RefreshToken(
            token=refresh_token,
            client_id=row.client_id,
            scopes=row.scopes,
            expires_at=row.expires_at,
            resource=row.resource,
            subject=str(row.user_id),
        )

    async def exchange_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: RefreshToken, scopes: list[str]
    ) -> OAuthToken:
        # Rotation: the whole grant goes, so the old access token dies with it.
        rotated = await self._one(
            "DELETE FROM oauth_tokens WHERE grant_id IN "
            "(SELECT grant_id FROM oauth_tokens WHERE token_hash = ? AND kind = 'refresh') RETURNING kind",
            (_hash(refresh_token.token),),
            write=True,
        )
        if rotated is None:
            raise TokenError("invalid_grant", "refresh token already used")
        assert refresh_token.subject is not None, "load_refresh_token sets the subject"
        return await self._issue(
            client.client_id or "", int(refresh_token.subject), scopes or refresh_token.scopes, refresh_token.resource
        )

    async def load_access_token(self, token: str) -> AccessToken | None:
        row = await self._token(token, "access")
        if row is None:
            return None
        return AccessToken(
            token=token,
            client_id=row.client_id,
            scopes=row.scopes,
            expires_at=row.expires_at,
            resource=row.resource,
            subject=str(row.user_id),
        )

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        await self._write(
            "DELETE FROM oauth_tokens WHERE grant_id IN (SELECT grant_id FROM oauth_tokens WHERE token_hash = ?)",
            (_hash(token.token),),
        )

    async def _issue(self, client_id: str, user_id: int, scopes: list[str], resource: str | None) -> OAuthToken:
        access, refresh, grant = secrets.token_urlsafe(32), secrets.token_urlsafe(32), secrets.token_urlsafe(16)
        now = int(time.time())
        rows = [
            (_hash(access), "access", grant, client_id, user_id, json.dumps(scopes), resource, now + ACCESS_TTL),
            (_hash(refresh), "refresh", grant, client_id, user_id, json.dumps(scopes), resource, now + REFRESH_TTL),
        ]
        await asyncio.to_thread(self._insert_tokens, rows)
        return OAuthToken(
            access_token=access,
            token_type="Bearer",
            expires_in=ACCESS_TTL,
            scope=" ".join(scopes) or None,
            refresh_token=refresh,
        )

    def _insert_tokens(self, rows: Sequence[tuple[object, ...]]) -> None:
        conn = connect(self._db_path)
        try:
            with transaction(conn):
                conn.executemany(
                    "INSERT INTO oauth_tokens "
                    "(token_hash, kind, grant_id, client_id, user_id, scopes, resource, expires_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    rows,
                )
        finally:
            conn.close()

    async def _token(self, token: str, kind: str) -> _TokenRow | None:
        row = await self._one(
            "SELECT client_id, user_id, scopes, resource, expires_at FROM oauth_tokens "
            "WHERE token_hash = ? AND kind = ? AND expires_at >= ?",
            (_hash(token), kind, int(time.time())),
        )
        if row is None:
            return None
        return _TokenRow(row[0], int(row[1]), json.loads(row[2]), row[3], int(row[4]))

    # SQLite

    async def _one(self, sql: str, params: tuple[object, ...], *, write: bool = False) -> tuple | None:
        return await asyncio.to_thread(self._run, sql, params, write)

    async def _write(self, sql: str, params: tuple[object, ...]) -> None:
        await asyncio.to_thread(self._run, sql, params, True)

    def _run(self, sql: str, params: tuple[object, ...], write: bool) -> tuple | None:
        """The first row of a single statement. fetchall finishes a RETURNING statement before COMMIT."""
        conn = connect(self._db_path)
        try:
            if write:
                with transaction(conn):
                    rows = conn.execute(sql, params).fetchall()
            else:
                rows = conn.execute(sql, params).fetchall()
            return tuple(rows[0]) if rows else None
        finally:
            conn.close()


@dataclass(frozen=True)
class _TokenRow:
    client_id: str
    user_id: int
    scopes: list[str]
    resource: str | None
    expires_at: int
