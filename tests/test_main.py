from types import SimpleNamespace

from barabot.__main__ import _announce_start
from barabot.core.db import connect, migrate
from barabot.core.logs import NOTICE
from barabot.domains.processor.budget import Budget


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
        _announce_start(cfg, Budget(db, limit_usd=None, price_per_m_input=1.0))
    # -1001 from both, -1009 from WATCH_CHATS, -1002 joined; the pending and the folder are not read
    assert "watching 3 chat(s)" in caplog.text
