"""Application database in data/barabot.sqlite.

DBOS keeps its own system database next to it. This one holds the
domain tables: `messages` written by the scraper, `listings` whose `rev`
the scraper bumps and whose content and `version` processing writes,
`subscriptions` with their `chats` written by the bot, and `matches`, the
decision processing makes for every listing version and subscription,
and `deliveries`, what the bot has shown each user. The `oauth_*` tables
belong to the MCP connector.

A listing's identity is derived by the schema, not by Python. Each
message row carries generated `listing_album` and `listing_ref` columns:
an album groups by `grouped_id`, a single message stands alone by
`msg_id`. Together with `chat_id` they form the primary key of
`listings`. The album flag keeps grouped_id and msg_id values apart.

Every caller opens a short-lived connection, usually inside
`asyncio.to_thread`, so no connection crosses threads.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import NamedTuple

# Shared by SCHEMA and the migration that rebuilds the table.
_SUBSCRIPTION_CHATS = """\
CREATE TABLE IF NOT EXISTS subscription_chats (
    subscription_id INTEGER NOT NULL REFERENCES subscriptions (id) ON DELETE CASCADE,
    chat            INTEGER NOT NULL REFERENCES chats (id),
    PRIMARY KEY (subscription_id, chat)
);
CREATE INDEX IF NOT EXISTS subscription_chats_by_chat ON subscription_chats (chat);
"""

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS messages (
    chat_id       INTEGER NOT NULL,
    msg_id        INTEGER NOT NULL,
    grouped_id    INTEGER,
    sender_id     INTEGER,
    date          TEXT    NOT NULL,
    text          TEXT    NOT NULL DEFAULT '',
    media         TEXT    NOT NULL DEFAULT '[]',
    deleted       INTEGER NOT NULL DEFAULT 0,
    listing_album INTEGER GENERATED ALWAYS AS (grouped_id IS NOT NULL) VIRTUAL,
    listing_ref   INTEGER GENERATED ALWAYS AS (coalesce(grouped_id, msg_id)) VIRTUAL,
    PRIMARY KEY (chat_id, msg_id)
);
-- Deletes from basic groups and private chats arrive without chat_id.
CREATE INDEX IF NOT EXISTS messages_by_msg_id ON messages (msg_id);
-- Assembly reads a listing's messages in msg_id order.
CREATE INDEX IF NOT EXISTS messages_by_listing
    ON messages (chat_id, listing_album, listing_ref, msg_id);

CREATE TABLE IF NOT EXISTS listings (
    chat_id      INTEGER NOT NULL,
    album        INTEGER NOT NULL,
    ref          INTEGER NOT NULL,
    rev          INTEGER NOT NULL,
    text         TEXT,
    media        TEXT,
    content_hash TEXT,
    version      INTEGER NOT NULL DEFAULT 0,
    removed      INTEGER NOT NULL DEFAULT 0,
    -- version whose matches are saved; lags version if a run stopped between steps
    decided_version INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (chat_id, album, ref)
);

-- A subscription, in the subscriber's words:
--   author      what the author of a wanted message does ("сдаёт жильё")
--   author_not  optional: the mirror role that must not match ("ищет жильё")
--   query       what the message is about ("квартира с балконом до 20 000 Kč")
CREATE TABLE IF NOT EXISTS subscriptions (
    id         INTEGER PRIMARY KEY,
    user_id    INTEGER NOT NULL,
    author     TEXT    NOT NULL,
    author_not TEXT,
    query      TEXT    NOT NULL,
    active     INTEGER NOT NULL DEFAULT 1,
    created_at TEXT    NOT NULL DEFAULT (datetime('now')),
    -- bumped when author, author_not or query change, not when chats do
    revision   INTEGER NOT NULL DEFAULT 1
);

-- A chat as a user named it for a subscription, see barabot/core/chats.py.
-- The bot adds the row; the scraper resolves it to chat_id, joins the chat
-- and updates status. kind and value hold a ChatRef:
--   username  value is the name in lower case, without @
--   invite    value is the hash of a t.me/+hash link
--   id        value is the marked chat ID, so chat_id is known from the start
CREATE TABLE IF NOT EXISTS chats (
    id         INTEGER PRIMARY KEY,
    kind       TEXT    NOT NULL CHECK (kind IN ('username', 'invite', 'id')),
    value      TEXT    NOT NULL,
    chat_id    INTEGER,
    title      TEXT,
    status     TEXT    NOT NULL DEFAULT 'pending'
               CHECK (status IN ('pending', 'joined', 'requested', 'failed')),
    updated_at TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE (kind, value)
);
CREATE INDEX IF NOT EXISTS chats_by_chat_id ON chats (chat_id);

-- A subscription only sees messages from the chats its user picked.
{_SUBSCRIPTION_CHATS}
-- One row per listing version and subscription that processing checked.
CREATE TABLE IF NOT EXISTS matches (
    chat_id         INTEGER NOT NULL,
    album           INTEGER NOT NULL,
    ref             INTEGER NOT NULL,
    version         INTEGER NOT NULL,
    subscription_id INTEGER NOT NULL,
    author_p        REAL    NOT NULL,
    choice          TEXT    NOT NULL,
    confidence      REAL    NOT NULL,
    round           INTEGER NOT NULL,
    matched         INTEGER NOT NULL,
    p_fits          REAL    NOT NULL DEFAULT 0,
    decided_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    -- subscriptions.revision whose text was checked
    subscription_revision INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (chat_id, album, ref, version, subscription_id)
);

-- What the bot last showed a user about a listing: one message per
-- listing and user, sent once and edited after that.
--   state    matched | unmatched | removed
--   version  the listing version the message shows
CREATE TABLE IF NOT EXISTS deliveries (
    chat_id        INTEGER NOT NULL,
    album          INTEGER NOT NULL,
    ref            INTEGER NOT NULL,
    user_id        INTEGER NOT NULL,
    version        INTEGER NOT NULL,
    state          TEXT    NOT NULL CHECK (state IN ('matched', 'unmatched', 'removed')),
    bot_message_id INTEGER NOT NULL,
    updated_at     TEXT    NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (chat_id, album, ref, user_id)
);

-- The MCP connector's OAuth state, see barabot/domains/mcp/oauth.py.
-- Codes and tokens are stored as SHA-256 hashes; times are Unix seconds.
CREATE TABLE IF NOT EXISTS oauth_clients (
    client_id  TEXT PRIMARY KEY,
    info       TEXT NOT NULL,  -- OAuthClientInformationFull as JSON
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
-- A login waiting for Telegram to call back; state is our state at Telegram.
CREATE TABLE IF NOT EXISTS oauth_logins (
    state      TEXT PRIMARY KEY,
    client_id  TEXT NOT NULL,
    request    TEXT NOT NULL,  -- the MCP client's AuthorizationParams as JSON
    verifier   TEXT NOT NULL,  -- PKCE verifier for Telegram
    nonce      TEXT NOT NULL,
    expires_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS oauth_codes (
    code_hash  TEXT PRIMARY KEY,
    client_id  TEXT    NOT NULL,
    user_id    INTEGER NOT NULL,
    request    TEXT    NOT NULL,
    expires_at REAL    NOT NULL
);
-- Access and refresh tokens; one grant is the pair issued together.
CREATE TABLE IF NOT EXISTS oauth_tokens (
    token_hash TEXT PRIMARY KEY,
    kind       TEXT    NOT NULL CHECK (kind IN ('access', 'refresh')),
    grant_id   TEXT    NOT NULL,
    client_id  TEXT    NOT NULL,
    user_id    INTEGER NOT NULL,
    scopes     TEXT    NOT NULL,  -- JSON list
    resource   TEXT,
    expires_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS oauth_tokens_by_grant ON oauth_tokens (grant_id);

-- One row per request to the decision model, for the spend limit and digests.
CREATE TABLE IF NOT EXISTS jev_usage (
    id            INTEGER PRIMARY KEY,
    at            TEXT    NOT NULL DEFAULT (datetime('now')),
    round         INTEGER NOT NULL,
    questions     INTEGER NOT NULL,
    photos        INTEGER NOT NULL,
    input_tokens  INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL
);
"""

# Columns added after a table first shipped: CREATE TABLE IF NOT EXISTS
# does not touch existing tables, so migrate() adds them.
_ADDED_COLUMNS = {
    "listings": {"decided_version": "INTEGER NOT NULL DEFAULT 0"},
    "matches": {"p_fits": "REAL NOT NULL DEFAULT 0", "subscription_revision": "INTEGER NOT NULL DEFAULT 1"},
    "subscriptions": {"revision": "INTEGER NOT NULL DEFAULT 1"},
}


class ListingKey(NamedTuple):
    chat_id: int
    album: bool
    ref: int

    @classmethod
    def from_row(cls, chat_id: int, album: int, ref: int) -> ListingKey:
        return cls(int(chat_id), bool(album), int(ref))

    def workflow_id(self, rev: int) -> str:
        """DBOS workflow IDs are strings; this is the only place a key becomes one."""
        kind = "a" if self.album else "m"
        return f"listing:{self.chat_id}:{kind}{self.ref}:{rev}"


def connect(path: Path) -> sqlite3.Connection:
    """Open the database in autocommit mode; use `transaction` for writes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def migrate(path: Path) -> None:
    conn = connect(path)
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA)
        for table, columns in _ADDED_COLUMNS.items():
            present = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
            for column, decl in columns.items():
                if column not in present:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
        _link_subscriptions_to_chats(conn)
    finally:
        conn.close()


def _link_subscriptions_to_chats(conn: sqlite3.Connection) -> None:
    """Rebuild subscription_chats of older code, which held Telegram chat IDs.

    Each old chat ID becomes a `chats` row of kind `id`, pending until the
    scraper confirms it is a member.
    """
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(subscription_chats)")}
    if "chat" in columns:
        return
    with transaction(conn):
        conn.execute(
            "INSERT OR IGNORE INTO chats (kind, value, chat_id) "
            "SELECT DISTINCT 'id', CAST(chat_id AS TEXT), chat_id FROM subscription_chats"
        )
        conn.execute("DROP INDEX IF EXISTS subscription_chats_by_chat")
        conn.execute("ALTER TABLE subscription_chats RENAME TO subscription_chats_old")
        for statement in _SUBSCRIPTION_CHATS.split(";")[:-1]:
            conn.execute(statement)
        conn.execute(
            "INSERT INTO subscription_chats (subscription_id, chat) "
            "SELECT o.subscription_id, c.id FROM subscription_chats_old o "
            "JOIN chats c ON c.kind = 'id' AND c.value = CAST(o.chat_id AS TEXT)"
        )
        conn.execute("DROP TABLE subscription_chats_old")


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Write transaction that takes the write lock up front.

    BEGIN IMMEDIATE avoids the deadlock where two readers both try to
    upgrade to writers; contention turns into busy_timeout waiting.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")
