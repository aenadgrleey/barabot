from pathlib import Path

import pytest

from barabot.core.db import ListingKey, connect, migrate


@pytest.fixture
def conn(tmp_path: Path):
    path = tmp_path / "nested" / "barabot.sqlite"
    migrate(path)
    connection = connect(path)
    yield connection
    connection.close()


def _insert(conn, chat_id, msg_id, grouped_id=None):
    conn.execute(
        "INSERT INTO messages (chat_id, msg_id, grouped_id, date) VALUES (?, ?, ?, '2026-11-23')",
        (chat_id, msg_id, grouped_id),
    )


def test_schema_derives_listing_key(conn):
    _insert(conn, -100, 7)
    _insert(conn, -100, 8, grouped_id=555)
    rows = conn.execute("SELECT msg_id, listing_album, listing_ref FROM messages ORDER BY msg_id").fetchall()
    assert [tuple(r) for r in rows] == [(7, 0, 7), (8, 1, 555)]


def test_album_flag_keeps_grouped_and_msg_ids_apart(conn):
    _insert(conn, -100, 555)
    _insert(conn, -100, 9, grouped_id=555)
    keys = {ListingKey.from_row(*r) for r in conn.execute("SELECT chat_id, listing_album, listing_ref FROM messages")}
    assert keys == {ListingKey(-100, False, 555), ListingKey(-100, True, 555)}


def test_workflow_id_encodes_kind():
    assert ListingKey(-100, True, 5).workflow_id(3) == "listing:-100:a5:3"
    assert ListingKey(-100, False, 5).workflow_id(3) == "listing:-100:m5:3"


@pytest.mark.parametrize(
    ("query", "index"),
    [
        ("SELECT * FROM messages WHERE msg_id IN (1, 2) AND chat_id > -1000000000000", "messages_by_msg_id"),
        (
            (
                "SELECT text FROM messages WHERE chat_id = 1 AND listing_album = 1 AND listing_ref = 2 "
                "AND deleted = 0 ORDER BY msg_id"
            ),
            "messages_by_listing",
        ),
    ],
    ids=["delete-without-chat", "assembly"],
)
def test_hot_queries_use_their_index(conn, query, index):
    plan = " ".join(row["detail"] for row in conn.execute(f"EXPLAIN QUERY PLAN {query}"))
    assert f"USING INDEX {index}" in plan
    assert "TEMP B-TREE" not in plan


def test_migrate_adds_columns_to_tables_created_by_older_code(tmp_path: Path):
    path = tmp_path / "old.sqlite"
    old = connect(path)
    old.execute(
        "CREATE TABLE listings (chat_id INTEGER, album INTEGER, ref INTEGER, rev INTEGER, text TEXT, "
        "media TEXT, content_hash TEXT, version INTEGER NOT NULL DEFAULT 0, removed INTEGER NOT NULL DEFAULT 0, "
        "PRIMARY KEY (chat_id, album, ref))"
    )
    old.execute("INSERT INTO listings (chat_id, album, ref, rev, version) VALUES (1, 0, 5, 3, 2)")
    old.close()

    migrate(path)
    migrate(path)  # idempotent

    conn = connect(path)
    row = conn.execute("SELECT version, decided_version FROM listings").fetchone()
    tables = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    conn.close()
    assert tuple(row) == (2, 0)
    assert {"subscriptions", "subscription_chats", "matches"} <= tables


def test_migrate_moves_old_subscription_chat_ids_into_chats(tmp_path: Path):
    path = tmp_path / "old.sqlite"
    old = connect(path)
    old.executescript(
        "CREATE TABLE subscriptions (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, author TEXT NOT NULL, "
        "author_not TEXT, query TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1, "
        "created_at TEXT NOT NULL DEFAULT (datetime('now')));"
        "CREATE TABLE subscription_chats (subscription_id INTEGER NOT NULL REFERENCES subscriptions (id) "
        "ON DELETE CASCADE, chat_id INTEGER NOT NULL, PRIMARY KEY (subscription_id, chat_id));"
        "CREATE INDEX subscription_chats_by_chat ON subscription_chats (chat_id);"
        "INSERT INTO subscriptions (id, user_id, author, query) VALUES (1, 5, 'a', 'q'), (2, 6, 'a', 'q');"
        "INSERT INTO subscription_chats VALUES (1, -100), (1, -200), (2, -100);"
    )
    old.close()

    migrate(path)
    migrate(path)  # idempotent

    conn = connect(path)
    links = conn.execute(
        "SELECT sc.subscription_id, c.kind, c.value, c.chat_id, c.status, s.revision FROM subscription_chats sc "
        "JOIN chats c ON c.id = sc.chat JOIN subscriptions s ON s.id = sc.subscription_id "
        "ORDER BY sc.subscription_id, c.chat_id"
    ).fetchall()
    tables = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    conn.close()
    assert [tuple(r) for r in links] == [
        (1, "id", "-200", -200, "pending", 1),
        (1, "id", "-100", -100, "pending", 1),
        (2, "id", "-100", -100, "pending", 1),
    ]
    assert "subscription_chats_old" not in tables
