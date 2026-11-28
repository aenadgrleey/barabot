from types import SimpleNamespace

from barabot.__main__ import _announce_start
from barabot.core.db import connect, migrate
from barabot.core.logs import NOTICE
from barabot.domains.budget import Budget


def test_start_counts_the_chats_the_scraper_reads(tmp_path, caplog):
    db = tmp_path / "barabot.sqlite"
    migrate(db)
    conn = connect(db)
    conn.execute(
        "INSERT INTO chats (kind, value, chat_id, status) VALUES "
        "('id', '-1001', -1001, 'joined'), ('username', 'flats', -1002, 'joined'), "
        "('username', 'bikes', -1003, 'pending'), ('list', 'slug', NULL, 'expanded')"
    )
    conn.close()
    cfg = SimpleNamespace(db_path=db, watch_chats=[-1001, -1009])
    with caplog.at_level(NOTICE):
        _announce_start(cfg, Budget(db, jev_limit_usd=None, jev_price_per_m_input=1.0))
    # -1001 from both, -1009 from WATCH_CHATS, -1002 joined; the pending and the folder are not read
    assert "watching 3 chat(s)" in caplog.text


def test_spend_command_prints_the_ledger_through_the_budget(tmp_path, monkeypatch, capsys):
    import argparse

    from barabot import __main__ as entry

    db = tmp_path / "barabot.sqlite"
    migrate(db)
    conn = connect(db)
    sub = conn.execute("INSERT INTO subscriptions (user_id, author, query) VALUES (42, 'a', 'q') RETURNING id")
    sub_id = sub.fetchone()["id"]
    conn.close()
    cfg = SimpleNamespace(db_path=db, jev_budget_usd=1.0, jev_price_per_m_input=1.0)
    monkeypatch.setattr(entry, "_load_config_or_exit", lambda: cfg)
    entry._build_budget(cfg).record_jev(
        subscription_ids=[sub_id], round=1, questions=1, photos=0, input_tokens=250_000, output_tokens=0
    )
    assert entry._spend_command(argparse.Namespace(days=7)) == 0
    out = capsys.readouterr().out
    assert "total $0.2500; Jev $0.2500 of $1.00" in out
    assert "42  $0.2500  1 request(s)" in out
