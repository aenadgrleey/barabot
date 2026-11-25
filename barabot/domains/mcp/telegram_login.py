"""Log In With Telegram: the OpenID Connect flow that tells us who the user is.

Telegram is the identity provider; barabot is its client, registered in
@BotFather under the bot's Login Widget, where the Client ID, the Client
Secret and the allowed redirect URLs live. See
https://core.telegram.org/bots/telegram-login.

We use Telegram once per login: send the browser to `authorize_url`, get
a code back on our callback, exchange it with `exchange_code` and read
the Telegram user ID from the signed ID token. Telegram's own access
token is thrown away; MCP clients get barabot's tokens (see `oauth.py`).

Scopes:
    openid               required
    profile              puts the numeric user ID in the `id` claim; `sub` is a different number
    telegram:bot_access  lets the bot message the user without /start

Network calls block; call them through `asyncio.to_thread`.
"""

from __future__ import annotations

import base64
import gzip
import hashlib
import json
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import jwt

ISSUER = "https://oauth.telegram.org"
AUTHORIZE_ENDPOINT = f"{ISSUER}/auth"
TOKEN_ENDPOINT = f"{ISSUER}/token"
JWKS_URI = f"{ISSUER}/.well-known/jwks.json"
SCOPES = ("openid", "profile", "telegram:bot_access")
# The algorithms that carry the profile scope; EdDSA and ES256K tokens cannot.
ALGORITHMS = ["RS256", "ES256"]


class TelegramLoginError(Exception):
    """The login did not prove who the user is. The message is safe to log."""


@dataclass(frozen=True)
class TelegramUser:
    id: int
    name: str | None
    username: str | None


@dataclass(frozen=True)
class LoginStart:
    """What the callback needs to finish a login it started."""

    url: str
    verifier: str  # PKCE verifier for Telegram's token endpoint
    nonce: str


def new_pkce() -> tuple[str, str]:
    """A PKCE verifier and its S256 challenge."""
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


class TelegramLogin:
    def __init__(
        self,
        client_id: str,
        client_secret: str,
        redirect_uri: str,
        *,
        opener: Callable[..., Any] = urlopen,
    ) -> None:
        self.client_id = client_id
        self._client_secret = client_secret
        self.redirect_uri = redirect_uri
        self._opener = opener
        self._keys = TelegramKeys(opener)

    def start(self, state: str) -> LoginStart:
        verifier, challenge = new_pkce()
        nonce = secrets.token_urlsafe(24)
        query = {
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "response_type": "code",
            "scope": " ".join(SCOPES),
            "state": state,
            "nonce": nonce,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
        return LoginStart(f"{AUTHORIZE_ENDPOINT}?{urlencode(query)}", verifier, nonce)

    def exchange_code(self, code: str, *, verifier: str, nonce: str) -> TelegramUser:
        """Trade the callback's code for an ID token and return the verified user."""
        basic = base64.b64encode(f"{self.client_id}:{self._client_secret}".encode()).decode()
        body = urlencode(
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": self.redirect_uri,
                "client_id": self.client_id,
                "code_verifier": verifier,
            }
        ).encode()
        request = Request(
            TOKEN_ENDPOINT,
            data=body,
            headers={
                "Authorization": f"Basic {basic}",
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
            },
        )
        try:
            with self._opener(request, timeout=10.0) as response:
                payload = read_json(response)
        except Exception as exc:
            # The HTTP error may echo the request; keep it out of the message.
            raise TelegramLoginError(f"Telegram token exchange failed: {type(exc).__name__}") from None
        id_token = payload.get("id_token") if isinstance(payload, dict) else None
        if not isinstance(id_token, str):
            raise TelegramLoginError("Telegram token response has no id_token")
        return self.verify_id_token(id_token, nonce=nonce)

    def verify_id_token(self, id_token: str, *, nonce: str) -> TelegramUser:
        try:
            key = self._keys.for_token(id_token)
            claims = jwt.decode(
                id_token,
                key.key,
                algorithms=ALGORITHMS,
                audience=self.client_id,
                issuer=ISSUER,
                options={"require": ["exp", "iat", "iss", "aud"]},
            )
        except jwt.PyJWTError as exc:
            raise TelegramLoginError(f"Telegram ID token is invalid: {exc}") from None
        # Telegram documents nonce for its JS library; check it whenever it comes back.
        if "nonce" in claims and claims["nonce"] != nonce:
            raise TelegramLoginError("Telegram ID token nonce does not match")
        user_id = claims.get("id")
        if isinstance(user_id, str) and user_id.isdigit():
            user_id = int(user_id)
        if isinstance(user_id, bool) or not isinstance(user_id, int) or user_id <= 0:
            raise TelegramLoginError("Telegram ID token has no user id; is the profile scope allowed?")
        name = claims.get("name")
        username = claims.get("preferred_username")
        return TelegramUser(
            id=user_id,
            name=name if isinstance(name, str) else None,
            username=username if isinstance(username, str) else None,
        )


class TelegramKeys:
    """Telegram's signing keys from jwks.json, fetched again when a token names an unknown kid.

    We fetch them through the same opener as the token exchange instead of PyJWKClient:
    oauth.telegram.org answers some servers with a gzip body that urllib does not unpack.
    """

    def __init__(self, opener: Callable[..., Any]) -> None:
        self._opener = opener
        self._keys: dict[str | None, jwt.PyJWK] = {}

    def for_token(self, id_token: str) -> jwt.PyJWK:
        kid = jwt.get_unverified_header(id_token).get("kid")
        if kid not in self._keys:
            self._keys = self._fetch()
        key = self._keys.get(kid)
        if key is None:
            raise jwt.PyJWKClientError(f"Telegram has no signing key {kid!r}")
        return key

    def _fetch(self) -> dict[str | None, jwt.PyJWK]:
        request = Request(JWKS_URI, headers={"Accept": "application/json"})
        try:
            with self._opener(request, timeout=10.0) as response:
                jwk_set = jwt.PyJWKSet.from_dict(read_json(response))
        except Exception as exc:
            raise TelegramLoginError(f"Telegram keys fetch failed: {type(exc).__name__}") from None
        return {key.key_id: key for key in jwk_set.keys}


def read_json(response: Any) -> Any:
    """The JSON body of a urllib response, unpacked when the server sent gzip without being asked."""
    raw = response.read()
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    return json.loads(raw)
