"""Offline smoke test: config + db + embed retry + cosine + lock.

Run with: cd ~/projects/barabot && .venv/bin/python smoke.py
"""
from __future__ import annotations

import asyncio
import math
import os
import socket
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


class _EmbedHandler(BaseHTTPRequestHandler):
    hits = 0
    fail_next = 0

    def do_POST(self):
        self.__class__.hits += 1
        if self.__class__.fail_next > 0:
            self.__class__.fail_next -= 1
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"error":"boom"}')
            return
        _ = self.rfile.read(int(self.headers["Content-Length"]))
        body = b'{"data":[{"embedding":[0.1,0.2,0.3,0.4,0.5,0.6,0.7,0.8]}]}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_a, **_kw):
        return


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def start_test_server() -> tuple[HTTPServer, int]:
    _EmbedHandler.hits = 0
    _EmbedHandler.fail_next = 0
    server = HTTPServer(("127.0.0.1", _free_port()), _EmbedHandler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, port


def with_env(url: str, extra: str = "") -> str:
    fd, path = tempfile.mkstemp(suffix=".env")
    os.write(fd, (
        "API_ID=1\n"
        "API_HASH=0123456789abcdef0123456789abcdef\n"
        "SESSION_STRING=stub\n"
        f"OPENAI_API_KEY=dummy\n"
        f"EMBEDDING_URL={url}\n"
        "EMBEDDING_MODEL=test-model\n"
        "SIMILARITY_THRESHOLD=0.85\n"
        "WATCH_CHATS=-1001,-1002\n"
        f"{extra}\n"
    ).encode())
    os.close(fd)
    return path


def check_config_validation() -> None:
    from dotenv import load_dotenv
    from barabot.config import load

    bad = with_env("http://x", "API_HASH=tooshort")
    load_dotenv(bad, override=True)
    try:
        load()
    except RuntimeError as e:
        assert "API_HASH" in str(e), e
    os.unlink(bad)

    bad = with_env("http://x", "SIMILARITY_THRESHOLD=1.5")
    load_dotenv(bad, override=True)
    try:
        load()
    except RuntimeError as e:
        assert "SIMILARITY_THRESHOLD" in str(e), e
    os.unlink(bad)

    bad = with_env("http://x", "LOG_LEVEL=LOUD")
    load_dotenv(bad, override=True)
    try:
        load()
    except RuntimeError as e:
        assert "LOG_LEVEL" in str(e), e
    os.unlink(bad)

    print("config validation ok")


def check_db() -> None:
    from barabot.db import (
        connect, iter_embeddings, iter_embeddings_sync, upsert, upsert_sync,
    )

    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / "x.sqlite"
        conn = connect(db)
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode.lower() == "wal", mode
        print("db WAL ok")

        v = [0.1] * 8
        upsert_sync(conn, chat_id=-1001, msg_id=10, text="x", embedding=v, sender="a", created_at="t")
        upsert_sync(conn, chat_id=-1001, msg_id=10, text="x2", embedding=v, sender="a", created_at="t2")
        rows = iter_embeddings_sync(conn, -1001)
        assert len(rows) == 1
        assert rows[0][1] == "x2"
        assert math.isclose(sum(rows[0][2]), sum(v), rel_tol=1e-5, abs_tol=1e-5)
        print("db sync upsert + roundtrip ok")

        async def a():
            await upsert(conn, chat_id=-1001, msg_id=20, text="y", embedding=v, sender="a", created_at="t")
            return await iter_embeddings(conn, -1001)
        rows = asyncio.run(a())
        assert len(rows) == 2
        print("db async wrappers ok")


def check_cosine() -> None:
    from barabot.similarity import best_match, cosine
    assert math.isclose(cosine([1, 0], [1, 0]), 1.0)
    assert math.isclose(cosine([1, 0], [0, 1]), 0.0)
    assert math.isclose(cosine([1, 0], [-1, 0]), -1.0)
    mid, score = best_match([1.0, 0.0], [])
    assert mid is None and score == 0.0
    print("cosine + best_match ok")


def check_embed_retry() -> None:
    from barabot.embed import embed, EmbeddingError

    server, port = start_test_server()
    url = f"http://127.0.0.1:{port}/v1/embeddings"
    try:
        _EmbedHandler.fail_next = 2
        vec = embed("hi", api_key="x", model="m", url=url, retries=3, base_delay=0.01, timeout=5)
        assert len(vec) == 8
        assert _EmbedHandler.hits == 3
        print("embed retry ok (3 hits, last succeeds)")

        _EmbedHandler.hits = 0
        _EmbedHandler.fail_next = 999
        try:
            embed("hi", api_key="x", model="m", url=url, retries=2, base_delay=0.01, timeout=5)
        except EmbeddingError as e:
            assert "after 3 attempts" in str(e), e
        assert _EmbedHandler.hits == 3
        print("embed retry exhaustion ok")
    finally:
        server.shutdown()


def check_embed_4xx_failfast() -> None:
    """4xx (other than 429) should NOT be retried."""
    from unittest.mock import patch as _patch
    from urllib.error import HTTPError

    from barabot.embed import embed, EmbeddingError

    calls = {"n": 0}

    def fake_urlopen(req, timeout=30):
        calls["n"] += 1
        raise HTTPError(req.full_url, 401, "Unauthorized", {}, None)

    with _patch("urllib.request.urlopen", fake_urlopen):
        try:
            embed("hi", api_key="x", model="m", url="http://x/v1/embeddings", retries=5, base_delay=0.01, timeout=5)
        except EmbeddingError as e:
            assert "client error 401" in str(e), e
    assert calls["n"] == 1, f"expected 1 call, got {calls['n']}"
    print("embed 4xx fail-fast ok")


def check_lock() -> None:
    from barabot.locks import _acquire_lock, _release_lock
    with tempfile.TemporaryDirectory() as td:
        lf = Path(td) / "lock"
        fd1 = _acquire_lock(lf)
        try:
            _acquire_lock(lf)
        except RuntimeError as e:
            assert "another barabot instance" in str(e), e
        else:
            raise AssertionError("expected second lock to fail")
        _release_lock(fd1, lf)
        fd2 = _acquire_lock(lf)
        _release_lock(fd2, lf)
    print("flock ok")


def check_media_group_coalesce() -> None:
    """Verify media-group (album) coalescing: several Message objects
    sharing a `grouped_id` should collapse to one UnifiedItem with
    photo_count = number of photos and text = the shared caption."""
    from barabot.media_group import coalesce

    class FakeSender:
        def __init__(self, username="alice"):
            self.username = username
            self.first_name = "A"
            self.last_name = ""

    class FakeMsg:
        def __init__(self, id, text, grouped_id=None, service=False, sender=None):
            self.id = id
            self.text = text
            self.grouped_id = grouped_id
            self.service = service
            self.sender = sender or FakeSender()

    # 4 photos in one album (grouped_id=42); only the last one carries the caption.
    msgs = [
        FakeMsg(100, "", grouped_id=42),
        FakeMsg(101, "", grouped_id=42),
        FakeMsg(102, "", grouped_id=42),
        FakeMsg(103, "Продам горный велосипед 1500 шек", grouped_id=42),
        # a singleton in between — separate listing
        FakeMsg(110, "Диван б/у, 5000 динар"),
        # a 2-photo album with NO caption anywhere — should be dropped entirely
        FakeMsg(120, "", grouped_id=99),
        FakeMsg(121, "", grouped_id=99),
        # another singleton
        FakeMsg(130, "Книги по 300"),
    ]
    items = coalesce(msgs, chat_id=1)
    assert len(items) == 3, f"expected 3 unified items, got {len(items)}: {items}"

    # Find the album item.
    album = next(i for i in items if i.msg_id == 100)
    assert album.photo_count == 4, f"album photo_count: {album.photo_count}"
    assert album.text == "Продам горный велосипед 1500 шек", f"album text: {album.text!r}"
    assert not album.is_service

    # Singletons preserved with photo_count=1.
    divan = next(i for i in items if i.msg_id == 110)
    assert divan.photo_count == 1 and divan.text == "Диван б/у, 5000 динар"

    books = next(i for i in items if i.msg_id == 130)
    assert books.photo_count == 1 and books.text == "Книги по 300"

    # The caption-less group of 2 was dropped (no text anywhere).
    for i in items:
        assert i.text, f"empty-text item leaked through: {i}"
    print("media_group coalesce ok")


def main() -> int:
    check_config_validation()
    check_db()
    check_cosine()
    check_embed_retry()
    check_embed_4xx_failfast()
    check_lock()
    check_media_group_coalesce()
    check_reporter_formatting()
    check_reporter_db_ro()
    check_reporter_config()
    check_reporter_poll_detects_new()
    print("\nALL OK")
    return 0


# --- reporter smoke --------------------------------------------------------

def with_reporter_env(extra: str = "") -> str:
    fd, path = tempfile.mkstemp(suffix=".env")
    os.write(fd, (
        "API_ID=1\n"
        "API_HASH=0123456789abcdef0123456789abcdef\n"
        "REPORTER_BOT_TOKEN=123456:abcdef\n"
        "ALLOWED_CHAT_IDS=100,200\n"
        "REPORTER_POLL_INTERVAL=0.5\n"
        "REPORTER_DEFAULT_LIMIT=5\n"
        f"{extra}\n"
    ).encode())
    os.close(fd)
    return path


def check_reporter_formatting() -> None:
    from reporter.formatting import (
        HELP, format_chat_counts, format_item, format_list, format_new,
    )
    item = {"chat_id": -1001, "msg_id": 7, "sender": "alice",
            "text": "продам велик", "created_at": "2026-06-23T10:00:00+00:00"}
    s = format_new(item, score=0.42)
    assert "🆕" in s and "-1001" in s and "alice" in s
    s2 = format_item(item)
    assert "-1001/7" in s2 and "alice" in s2
    s3 = format_list([item, item], "header")
    assert "header" in s3 and "1." in s3 and "2." in s3
    s4 = format_list([], "x")
    assert "(none yet)" in s4
    s5 = format_chat_counts([(-1001, 5), (-1002, 1)])
    assert "-1001" in s5 and "5" in s5
    assert HELP.startswith("🛒")
    print("reporter formatting ok")


def check_reporter_db_ro() -> None:
    """Reporter must see barabot's items, never write to them."""
    from barabot.db import connect, upsert_sync
    from reporter.db import (
        chat_counts, connect_ro, fetch_recent, fetch_since, get_item,
        max_rowid, search,
    )

    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / "x.sqlite"
        c = connect(db)
        v = [0.0] * 8
        upsert_sync(c, chat_id=-1001, msg_id=1, text="first", embedding=v, sender="a", created_at="t")
        upsert_sync(c, chat_id=-1001, msg_id=2, text="second", embedding=v, sender="a", created_at="t")
        upsert_sync(c, chat_id=-1002, msg_id=3, text="third", embedding=v, sender="b", created_at="t")

        rc = connect_ro(db)
        try:
            assert max_rowid(rc) == 3
            recent = fetch_recent(rc, 2)
            assert len(recent) == 2
            assert recent[0]["msg_id"] == 3  # newest first

            after1 = fetch_since(rc, 1)
            assert [r["msg_id"] for r in after1] == [2, 3]
            assert all(r["rowid"] > 1 for r in after1)

            counts = chat_counts(rc)
            assert counts == [(-1001, 2), (-1002, 1)]

            hits = search(rc, "sec")
            assert len(hits) == 1 and hits[0]["msg_id"] == 2

            found = get_item(rc, -1001, 1)
            assert found and found["text"] == "first"
            assert get_item(rc, -1001, 999) is None
        finally:
            rc.close()
    print("reporter db (ro) ok")


def check_reporter_config() -> None:
    from dotenv import load_dotenv
    from reporter.config import load as rload

    def _reset(extra: str = "") -> str:
        # Wipe all env vars that affect barabot/reporter, then load a fresh
        # .env from a temp file. Both configs read the same env, so they
        # need a clean slate or stale LOG_LEVEL/REPORTER_POLL_INTERVAL
        # bleed from one test into the next.
        for k in list(os.environ):
            if k in {
                "API_ID", "API_HASH", "REPORTER_BOT_TOKEN", "ALLOWED_CHAT_IDS",
                "REPORTER_POLL_INTERVAL", "REPORTER_DEFAULT_LIMIT",
                "LOG_LEVEL", "EMBEDDING_URL", "EMBEDDING_MODEL",
                "SIMILARITY_THRESHOLD", "WATCH_CHATS", "EMBEDDING_CONCURRENCY",
                "EMBEDDING_RETRIES", "EMBEDDING_TIMEOUT",
                "OPENAI_API_KEY", "SESSION_STRING",
            }:
                del os.environ[k]
        p = with_reporter_env(extra)
        load_dotenv(p, override=True)
        return p

    p = _reset("REPORTER_POLL_INTERVAL=0.1")
    try:
        rload()
    except RuntimeError as e:
        assert "REPORTER_POLL_INTERVAL" in str(e), e
    os.unlink(p)

    p = _reset("REPORTER_DEFAULT_LIMIT=200")
    try:
        rload()
    except RuntimeError as e:
        assert "REPORTER_DEFAULT_LIMIT" in str(e), e
    os.unlink(p)

    p = _reset()
    cfg = rload()
    assert cfg.poll_interval == 0.5
    assert cfg.allowed_chat_ids == [100, 200]
    assert cfg.default_limit == 5
    os.unlink(p)
    print("reporter config ok")


def check_reporter_poll_detects_new() -> None:
    """Simulate 'barabot inserts, reporter polls' without Telegram.

    We replicate the reporter's poll loop in-process: it tracks last_rowid,
    fetch_since returns new rows, and we assert they flow through.
    """
    from barabot.db import connect, upsert_sync
    from reporter.db import connect_ro, fetch_since, max_rowid

    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / "x.sqlite"
        c = connect(db)
        rc = connect_ro(db)
        try:
            v = [0.0] * 8
            upsert_sync(c, chat_id=-1001, msg_id=1, text="a", embedding=v, sender="x", created_at="t")
            last = max_rowid(rc)
            assert last == 1
            assert fetch_since(rc, last) == []

            upsert_sync(c, chat_id=-1001, msg_id=2, text="b", embedding=v, sender="x", created_at="t")
            upsert_sync(c, chat_id=-1002, msg_id=3, text="c", embedding=v, sender="x", created_at="t")
            new = fetch_since(rc, last)
            assert [r["msg_id"] for r in new] == [2, 3]
            last = max_rowid(rc)
            assert fetch_since(rc, last) == []
        finally:
            rc.close()
    print("reporter poll detection ok")


if __name__ == "__main__":
    sys.exit(main())
