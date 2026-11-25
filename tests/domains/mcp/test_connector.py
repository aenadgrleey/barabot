"""The MCP connector end to end: OAuth with a fake Telegram, then tools over /mcp."""

import base64
import hashlib
import io
import json
import time
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from starlette.testclient import TestClient

from barabot.core.db import migrate
from barabot.domains.bot import subscriptions
from barabot.domains.mcp.server import CALLBACK_PATH, build_app
from barabot.domains.mcp.telegram_login import ISSUER, TelegramLogin, TelegramLoginError, TelegramUser

PUBLIC = "https://barabot.example"
BOT_ID = "777000"
CLIENT_REDIRECT = "http://localhost:6274/callback"
USER_ID = 4242
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def id_token(**claims):
    now = int(time.time())
    body = {"iss": ISSUER, "aud": BOT_ID, "sub": "9" * 19, "iat": now, "exp": now + 600, "id": USER_ID}
    return jwt.encode({**body, **claims}, KEY, algorithm="RS256")


class FakeTelegram:
    """Stands in for oauth.telegram.org: the token endpoint and its keys."""

    def __init__(self):
        self.token = id_token()
        self.requests = []

    def opener(self, request, timeout):
        self.requests.append(request)
        return io.BytesIO(json.dumps({"access_token": "tg", "token_type": "Bearer", "id_token": self.token}).encode())

    def get_signing_key_from_jwt(self, _token):
        return SimpleNamespace(key=KEY.public_key())


@pytest.fixture
def telegram():
    return FakeTelegram()


@pytest.fixture
def login(telegram):
    return TelegramLogin(BOT_ID, "secret", PUBLIC + CALLBACK_PATH, opener=telegram.opener, jwks=telegram)  # pyright: ignore[reportArgumentType]


@pytest.fixture
def greeted():
    return []


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "barabot.sqlite"
    migrate(path)
    return path


@pytest.fixture
def client(db, login, greeted):
    class Bot:
        async def send(self, user_id: int, text: str) -> int:
            greeted.append(user_id)
            return 1

        async def edit(self, user_id: int, message_id: int, text: str) -> None:
            raise AssertionError("the connector only sends")

    app = build_app(db, login, Bot(), PUBLIC, port=8765)
    with TestClient(app, base_url=PUBLIC, follow_redirects=False) as test_client:
        yield test_client


def pkce():
    verifier = "v" * 64
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def query(url):
    return {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}


def connect(client):
    """Register, log in through Telegram and get tokens, as Claude would."""
    reg = client.post("/register", json={"redirect_uris": [CLIENT_REDIRECT], "token_endpoint_auth_method": "none"})
    assert reg.status_code == 201, reg.text
    client_id = reg.json()["client_id"]
    verifier, challenge = pkce()

    auth = client.get(
        "/authorize",
        params={
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": CLIENT_REDIRECT,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": "client-state",
        },
    )
    assert auth.status_code == 302, auth.text
    to_telegram = auth.headers["location"]
    assert to_telegram.startswith("https://oauth.telegram.org/auth?")
    tg = query(to_telegram)
    assert tg["client_id"] == BOT_ID
    assert tg["redirect_uri"] == PUBLIC + CALLBACK_PATH
    assert set(tg["scope"].split()) == {"openid", "profile", "telegram:bot_access"}

    back = client.get(CALLBACK_PATH, params={"code": "telegram-code", "state": tg["state"]})
    assert back.status_code == 302, back.text
    to_client = query(back.headers["location"])
    assert back.headers["location"].startswith(CLIENT_REDIRECT)
    assert to_client["state"] == "client-state"

    tokens = client.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": to_client["code"],
            "redirect_uri": CLIENT_REDIRECT,
            "client_id": client_id,
            "code_verifier": verifier,
        },
    )
    assert tokens.status_code == 200, tokens.text
    return client_id, tokens.json(), to_client["code"], verifier


def rpc(client, token, method, params=None, id_=1):
    response = client.post(
        "/mcp",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": "2025-11-25",
        },
        json={"jsonrpc": "2.0", "id": id_, "method": method, "params": params or {}},
    )
    return response


def call_tool(client, token, name, arguments):
    response = rpc(client, token, "tools/call", {"name": name, "arguments": arguments})
    assert response.status_code == 200, response.text
    return response.json()["result"]


def test_mcp_requires_a_token_and_points_to_metadata(client):
    response = rpc(client, "nope", "tools/list")
    assert response.status_code == 401
    assert "oauth-protected-resource" in response.headers["www-authenticate"]
    meta = client.get("/.well-known/oauth-authorization-server").json()
    assert meta["issuer"] == PUBLIC
    assert meta["registration_endpoint"] == PUBLIC + "/register"


def test_full_flow_logs_in_through_telegram_and_manages_own_subscriptions(client, db, telegram, greeted):
    _, tokens, _, _ = connect(client)
    assert greeted == [USER_ID]
    # Telegram got our PKCE verifier and the bot's credentials.
    sent = parse_qs(telegram.requests[0].data.decode())
    assert sent["code"] == ["telegram-code"] and "code_verifier" in sent
    assert telegram.requests[0].headers["Authorization"].startswith("Basic ")

    access = tokens["access_token"]
    names = {tool["name"] for tool in rpc(client, access, "tools/list").json()["result"]["tools"]}
    assert names == {"list_subscriptions", "add_subscription", "update_subscription", "remove_subscription"}

    added = call_tool(
        client,
        access,
        "add_subscription",
        {"author": "сдаёт квартиру", "author_not": "ищет квартиру", "query": "2 комнаты", "chats": ["@prague_flats"]},
    )
    sub = added["structuredContent"]
    assert sub["chats"] == [{"chat": "@prague_flats", "title": None, "status": "pending"}]

    updated = call_tool(client, access, "update_subscription", {"subscription_id": sub["id"], "author_not": ""})
    assert updated["structuredContent"]["author_not"] is None
    assert updated["structuredContent"]["query"] == "2 комнаты"

    listed = call_tool(client, access, "list_subscriptions", {})["structuredContent"]["subscriptions"]
    assert [s["id"] for s in listed] == [sub["id"]]

    # The subscription belongs to the Telegram user from the ID token.
    assert [row.user_id for row in subscriptions.list_all(db)] == [USER_ID]

    bad = call_tool(client, access, "add_subscription", {"author": "x", "query": "y", "chats": ["https://example.com"]})
    assert bad["isError"] is True
    assert "not a Telegram chat" in bad["content"][0]["text"]

    call_tool(client, access, "remove_subscription", {"subscription_id": sub["id"]})
    gone = call_tool(client, access, "remove_subscription", {"subscription_id": sub["id"]})
    assert gone["isError"] is True


def test_code_is_single_use_and_refresh_rotates(client):
    client_id, tokens, code, verifier = connect(client)
    again = client.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": CLIENT_REDIRECT,
            "client_id": client_id,
            "code_verifier": verifier,
        },
    )
    assert again.status_code == 400

    refresh = {"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"], "client_id": client_id}
    fresh = client.post("/token", data=refresh)
    assert fresh.status_code == 200, fresh.text
    assert client.post("/token", data=refresh).status_code == 400
    # The old access token died with its grant; the new one works.
    assert rpc(client, tokens["access_token"], "tools/list").status_code == 401
    assert rpc(client, fresh.json()["access_token"], "tools/list").status_code == 200


def test_declined_or_failed_login_returns_an_error_to_the_client(client, telegram, greeted):
    reg = client.post("/register", json={"redirect_uris": [CLIENT_REDIRECT], "token_endpoint_auth_method": "none"})
    client_id = reg.json()["client_id"]
    _, challenge = pkce()
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": CLIENT_REDIRECT,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": "s",
    }
    state = query(client.get("/authorize", params=params).headers["location"])["state"]
    declined = client.get(CALLBACK_PATH, params={"error": "access_denied", "state": state})
    assert query(declined.headers["location"]) == {"error": "access_denied", "state": "s"}

    telegram.token = id_token(aud="someone-else")
    state = query(client.get("/authorize", params=params).headers["location"])["state"]
    failed = client.get(CALLBACK_PATH, params={"code": "c", "state": state})
    assert query(failed.headers["location"])["error"] == "access_denied"
    assert greeted == []

    # A state is used once.
    assert client.get(CALLBACK_PATH, params={"code": "c", "state": state}).status_code == 400


@pytest.mark.parametrize(
    ("claims", "nonce"),
    [({"aud": "other"}, "n"), ({"iss": "https://evil"}, "n"), ({"nonce": "x"}, "n"), ({"id": None}, "n")],
)
def test_id_token_checks(login, claims, nonce):
    with pytest.raises(TelegramLoginError):
        login.verify_id_token(id_token(**claims), nonce=nonce)


def test_id_token_gives_the_numeric_user_id_not_sub(login):
    user = login.verify_id_token(id_token(name="Nash", preferred_username="nash"), nonce="n")
    assert user == TelegramUser(id=USER_ID, name="Nash", username="nash")
