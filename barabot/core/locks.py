"""Single-instance lock and PID file helpers.

Lifted out of bot.py so unit tests can verify them without importing
Telethon (which on import touches asyncio.get_event_loop and crashes
if there's no running loop).
"""
from __future__ import annotations

import contextlib
import fcntl
import os
from pathlib import Path


def _acquire_lock(path: Path) -> int:
    """Open the lock file and take an exclusive fcntl lock. Returns the fd.

    Raises RuntimeError if another instance already holds it.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        os.close(fd)
        raise RuntimeError(
            f"another barabot instance is already running (lock at {path})"
        ) from exc
    return fd


def _release_lock(fd: int, path: Path) -> None:
    with contextlib.suppress(OSError):
        fcntl.flock(fd, fcntl.LOCK_UN)
    with contextlib.suppress(OSError):
        os.close(fd)
    # Leave the lock file in place — it's just a marker; next instance reuses it.


def _write_pid(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(os.getpid()))


def _remove_pid(path: Path) -> None:
    with contextlib.suppress(FileNotFoundError):
        path.unlink()
