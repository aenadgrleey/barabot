"""Minimal application database connection.

The database intentionally has no schema, migrations, or queue behavior yet.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path


def connect(path: Path) -> sqlite3.Connection:
    """Open the application database, creating its parent directory if needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    return sqlite3.connect(path)
