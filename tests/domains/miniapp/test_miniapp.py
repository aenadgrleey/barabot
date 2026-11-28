"""The Mini App: initData checks, the read-only subscriptions API, and its place next to the MCP connector."""

import json
import time
from urllib.parse import urlencode

import pytest
from starlette.testclient import TestClient

from barabot.api import BarabotApi
from barabot.core.db import connect, migrate
from barabot.core.web import compose
from barabot.domains.bot import reach, subscriptions
from barabot.domains.mcp import server as mcp
from barabot.domains.mcp.telegram_login import TelegramLogin
from barabot.domains.miniapp import init_data
from barabot.domains.miniapp.routes import PATH, build_app, page_url

PUBLIC = "https://barabot.example"
TOKEN = "777000:bot-secret"
USER_ID = 4242


def make_init_data(user_id=USER_ID, *, token=TOKEN, auth_date=None, **extra):
    fields = {
        "auth_date": str(int(time.time()) if auth_date is None else auth_date),
        "query_id": "AAH",
        "user": json.dumps({"id": user_id, "first_name": "Nash"}),
        **extra,
    }
    return urlencode({**fields, "hash": init_data.sign(fields, token)})


def test_valid_init_data_gives_the_user_id():
    assert init_data.verify(make_init_data(), TOKEN) == USER_ID


@pytest.mark.parametrize(
    "raw",
    [
        make_init_data(token="777000:other-bot"),
        make_init_data(auth_date=int(time.time()) - init_data.MAX_AGE_SECONDS - 60),
        make_init_data().replace("Nash", "Eve"),
        "user=%7B%22id%22%3A1%7D&auth_date=1",
        "",
    ],
    ids=["other-bot", "expired", "tampered", "unsigned", "empty"],
)
def test_bad_init_data_is_refused(raw):
    with pytest.raises(init_data.InvalidInitData):
        init_data.verify(raw, TOKEN)


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "barabot.sqlite"
    migrate(path)
    return path


@pytest.fixture
def client(db):
    async def bot_link() -> str:
        return "https://t.me/barabot_test_bot"

    app = build_app(BarabotApi(db), db, db.parent / "photos", TOKEN, bot_link)
    with TestClient(app, base_url=PUBLIC) as test_client:
        yield test_client


def test_page_is_served_without_auth(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "telegram-web-app.js" in response.text
    assert "Add subscription" in response.text


def test_lists_only_the_users_own_subscriptions(client, db):
    subscriptions.add(db, user_id=USER_ID, author="сдаёт квартиру", query="2 комнаты", chats=["@flats"])
    subscriptions.add(db, user_id=999, author="продаёт", query="велосипед", chats=["@bikes"])

    response = client.get("/api/subscriptions", headers={"Authorization": "tma " + make_init_data()})

    assert response.status_code == 200
    body = response.json()
    assert [s["query"] for s in body["subscriptions"]] == ["2 комнаты"]
    assert body["subscriptions"][0]["chats"] == [
        {"chat": "@flats", "title": None, "username": None, "photo": None, "status": "pending", "folder": None}
    ]
    assert body["notifications_blocked"] is False
    assert body["bot_link"] == "https://t.me/barabot_test_bot"


def _give_photo(db, value, name):
    (db.parent / "photos").mkdir(exist_ok=True)
    (db.parent / "photos" / name).write_bytes(b"jpeg")
    conn = connect(db)
    conn.execute("UPDATE chats SET username = 'Flats', photo = ? WHERE value = ?", (name, value))
    conn.commit()
    conn.close()


def test_chat_photos_are_served_only_to_users_who_watch_the_chat(client, db):
    subscriptions.add(db, user_id=USER_ID, author="a", query="q", chats=["@flats"])
    subscriptions.add(db, user_id=999, author="b", query="q", chats=["@bikes"])
    _give_photo(db, "flats", "11.jpg")
    _give_photo(db, "bikes", "22.jpg")
    auth = {"Authorization": "tma " + make_init_data()}

    chat = client.get("/api/subscriptions", headers=auth).json()["subscriptions"][0]["chats"][0]
    assert (chat["username"], chat["photo"]) == ("Flats", "api/photos/11.jpg")

    response = client.get("/api/photos/11.jpg", headers=auth)
    assert response.status_code == 200
    assert response.content == b"jpeg"
    assert response.headers["content-type"] == "image/jpeg"
    assert client.get("/api/photos/11.jpg").status_code == 401
    assert client.get("/api/photos/22.jpg", headers=auth).status_code == 404  # someone else's chat
    assert client.get("/api/photos/33.jpg", headers=auth).status_code == 404
    assert client.get("/api/photos/..%2Fbarabot.sqlite", headers=auth).status_code == 404


def test_reports_when_the_bot_cannot_write(client, db):
    reach.mark_unreachable(db, USER_ID, "UserIsBlockedError")
    response = client.get("/api/subscriptions", headers={"Authorization": "tma " + make_init_data()})
    assert response.json()["notifications_blocked"] is True


@pytest.mark.parametrize(
    "header",
    [None, "Bearer abc", "tma ", "tma " + make_init_data(token="777000:other-bot")],
    ids=["none", "bearer", "empty", "other-bot"],
)
def test_api_refuses_requests_without_valid_init_data(client, header):
    headers = {"Authorization": header} if header else {}
    response = client.get("/api/subscriptions", headers=headers)
    assert response.status_code == 401
    assert "error" in response.json()


def test_served_under_app_next_to_the_mcp_connector(db):
    class Bot:
        async def send(self, user_id: int, text: str) -> int:
            raise AssertionError("not sent here")

        async def edit(self, user_id: int, message_id: int, text: str) -> None:
            raise AssertionError("not sent here")

        async def link(self) -> str:
            return "https://t.me/barabot_test_bot"

    login = TelegramLogin("777000", "secret", PUBLIC + mcp.CALLBACK_PATH)
    app = compose(
        [
            (PATH, build_app(BarabotApi(db), db, db.parent / "photos", TOKEN, Bot().link)),
            ("", mcp.build_app(db, login, Bot(), PUBLIC, port=8765)),
        ]
    )
    with TestClient(app, base_url=PUBLIC, follow_redirects=False) as client:
        assert page_url(PUBLIC) == PUBLIC + "/app/"
        assert client.get("/app").headers["location"] == "/app/"
        assert "Add subscription" in client.get("/app/").text
        # The page fetches "api/subscriptions" relative to /app/.
        response = client.get("/app/api/subscriptions", headers={"Authorization": "tma " + make_init_data()})
        assert response.json()["subscriptions"] == []
        assert client.get("/.well-known/oauth-authorization-server").json()["issuer"].startswith(PUBLIC)
        assert client.post("/mcp", json={}).status_code == 401
