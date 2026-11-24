import sqlite3

import pytest

from barabot.__main__ import main
from barabot.core.chats import ChatId, ChatStatus, Invite, Username, parse_chat, ref_from_row, ref_to_row
from barabot.core.db import connect, migrate
from barabot.domains.bot import subscriptions


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "barabot.sqlite"
    migrate(path)
    return path


def test_add_list_deactivate(db):
    sub_id = subscriptions.add(
        db, user_id=5, author=" сдаёт жильё ", author_not="", query="квартира с балконом", chats=[-100, -200, -100]
    )
    [row] = subscriptions.list_all(db)
    assert (row.id, row.user_id, row.author, row.author_not, row.active) == (sub_id, 5, "сдаёт жильё", None, True)
    assert [(c.ref, c.chat_id, c.status) for c in row.chats] == [
        (ChatId(-100), -100, ChatStatus.PENDING),
        (ChatId(-200), -200, ChatStatus.PENDING),
    ]
    assert subscriptions.deactivate(db, sub_id)
    assert not subscriptions.deactivate(db, sub_id)
    assert not subscriptions.list_all(db)[0].active


@pytest.mark.parametrize(
    "kwargs",
    [
        {"author": "", "query": "q", "chats": [1]},
        {"author": "a", "query": " ", "chats": [1]},
        {"author": "a", "query": "q", "chats": []},
        {"author": "a", "query": "q", "chats": ["not a chat!"]},
        {"author": "a", "query": "q", "chats": [42]},
    ],
)
def test_add_rejects_incomplete_subscriptions(db, kwargs):
    with pytest.raises(ValueError):
        subscriptions.add(db, user_id=1, **kwargs)


@pytest.mark.parametrize(
    ("raw", "ref"),
    [
        ("@NSbaraholka", Username("nsbaraholka")),
        ("nsbaraholka", Username("nsbaraholka")),
        ("https://t.me/NSbaraholka", Username("nsbaraholka")),
        ("t.me/nsbaraholka/12345", Username("nsbaraholka")),
        ("https://t.me/s/nsbaraholka", Username("nsbaraholka")),
        ("https://t.me/+AbC-12_x", Invite("AbC-12_x")),
        ("https://t.me/joinchat/AbC12", Invite("AbC12")),
        (" -1001234567890 ", ChatId(-1001234567890)),
        (-1001234567890, ChatId(-1001234567890)),
    ],
)
def test_parse_chat(raw, ref):
    assert parse_chat(raw) == ref


@pytest.mark.parametrize("ref", [Username("chat_one"), Invite("AbC-12_x"), ChatId(-1001234567890)])
def test_chat_ref_round_trips_through_its_row(ref):
    assert ref_from_row(*ref_to_row(ref)) == ref


def test_chats_reject_a_kind_or_status_outside_the_schema(db):
    conn = connect(db)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO chats (kind, value) VALUES ('link', 'x')")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO chats (kind, value, status) VALUES ('username', 'x', 'done')")
    conn.close()


def test_one_chat_named_twice_is_linked_once(db):
    sub_id = subscriptions.add(db, user_id=1, author="a", query="q", chats=["@Chat_one", "https://t.me/chat_one"])
    other = subscriptions.add(db, user_id=2, author="a", query="q", chats=["chat_one"])
    [row] = subscriptions.list_all(db, user_id=1)
    assert [c.ref for c in row.chats] == [Username("chat_one")]
    conn = connect(db)
    assert conn.execute("SELECT count(*) FROM chats").fetchone()[0] == 1
    conn.close()
    assert sub_id != other


def test_cli_round_trip(tmp_path, monkeypatch, capsys):
    # Patch by path: other tests re-import barabot.core.config, and the CLI
    # imports DATA_DIR from whatever module object is current.
    monkeypatch.setattr("barabot.core.config.DATA_DIR", tmp_path)
    assert (
        main(
            [
                "subscription",
                "add",
                "--user",
                "5",
                "--chat",
                "-100",
                "--chat",
                "@prague_flats",
                "--author",
                "ищет жильё, чтобы снять",
                "--author-not",
                "сдаёт или продаёт жильё",
                "--query",
                "квартира 2+kk в Праге",
            ]
        )
        == 0
    )
    assert main(["subscription", "list"]) == 0
    out = capsys.readouterr().out
    assert "subscription 1 created" in out
    assert "chats=-100(pending),@prague_flats(pending)" in out
    assert "not: сдаёт или продаёт жильё" in out and "query: квартира 2+kk в Праге" in out
    assert main(["subscription", "remove", "1"]) == 0
    assert main(["subscription", "remove", "1"]) == 1
    assert (tmp_path / "barabot.sqlite").exists()


def test_redecide_marks_listings_undecided(tmp_path, monkeypatch, capsys):

    monkeypatch.setattr("barabot.core.config.DATA_DIR", tmp_path)
    path = tmp_path / "barabot.sqlite"
    migrate(path)
    conn = connect(path)
    conn.execute(
        "INSERT INTO listings (chat_id, album, ref, rev, version, decided_version) VALUES (-1, 0, 1, 1, 1, 1), (-1, 0, 2, 1, 0, 0)"
    )
    conn.close()
    assert main(["processor", "redecide"]) == 0
    assert "1 listing(s) marked undecided" in capsys.readouterr().out
    conn = connect(path)
    assert conn.execute("SELECT max(decided_version) FROM listings").fetchone()[0] == 0
    conn.close()
