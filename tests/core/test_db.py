from pathlib import Path

from barabot.core.db import connect


def test_connect_creates_a_usable_database(tmp_path: Path) -> None:
    database_path = tmp_path / "nested" / "barabot.sqlite"

    connection = connect(database_path)
    try:
        assert database_path.exists()
        assert connection.execute("SELECT 1").fetchone() == (1,)
    finally:
        connection.close()
