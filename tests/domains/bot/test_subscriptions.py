import pytest

from barabot.__main__ import main
from barabot.core.db import migrate
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
    assert sorted(row.chats) == [-200, -100]
    assert subscriptions.deactivate(db, sub_id)
    assert not subscriptions.deactivate(db, sub_id)
    assert not subscriptions.list_all(db)[0].active


@pytest.mark.parametrize(
    "kwargs",
    [
        {"author": "", "query": "q", "chats": [1]},
        {"author": "a", "query": " ", "chats": [1]},
        {"author": "a", "query": "q", "chats": []},
    ],
)
def test_add_rejects_incomplete_subscriptions(db, kwargs):
    with pytest.raises(ValueError):
        subscriptions.add(db, user_id=1, **kwargs)


def test_cli_round_trip(tmp_path, monkeypatch, capsys):
    # Patch by path: other tests re-import barabot.core.config, and the CLI
    # imports DATA_DIR from whatever module object is current.
    monkeypatch.setattr("barabot.core.config.DATA_DIR", tmp_path)
    assert main([
        "subscription", "add", "--user", "5", "--chat", "-100", "--chat", "-200",
        "--author", "ищет жильё, чтобы снять", "--author-not", "сдаёт или продаёт жильё",
        "--query", "квартира 2+kk в Праге",
    ]) == 0
    assert main(["subscription", "list"]) == 0
    out = capsys.readouterr().out
    assert "subscription 1 created" in out
    assert "not: сдаёт или продаёт жильё" in out and "query: квартира 2+kk в Праге" in out
    assert main(["subscription", "remove", "1"]) == 0
    assert main(["subscription", "remove", "1"]) == 1
    assert (tmp_path / "barabot.sqlite").exists()


def test_redecide_marks_listings_undecided(tmp_path, monkeypatch, capsys):
    from barabot.core.db import connect, migrate

    monkeypatch.setattr("barabot.core.config.DATA_DIR", tmp_path)
    path = tmp_path / "barabot.sqlite"
    migrate(path)
    conn = connect(path)
    conn.execute("INSERT INTO listings (chat_id, album, ref, rev, version, decided_version) VALUES (-1, 0, 1, 1, 1, 1), (-1, 0, 2, 1, 0, 0)")
    conn.close()
    assert main(["processor", "redecide"]) == 0
    assert "1 listing(s) marked undecided" in capsys.readouterr().out
    conn = connect(path)
    assert conn.execute("SELECT max(decided_version) FROM listings").fetchone()[0] == 0
    conn.close()
