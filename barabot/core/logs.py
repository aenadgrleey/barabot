"""Logging that aenaut-notify can route by priority.

On aenaut every service only logs. aenaut-notify reads journald and sends
lines of priority notice (5) and above to Telegram. Under systemd a line's
priority comes from a "<N>" prefix on stdout, so in that case every line
gets one. NOTICE is the level for "normal but significant" events a human
should see: startup, a match, the hourly digest, budget milestones.
"""
from __future__ import annotations

import logging
import os
import sys

NOTICE = 25  # between INFO and WARNING, like syslog's notice
logging.addLevelName(NOTICE, "NOTICE")

_SYSLOG = {logging.CRITICAL: 2, logging.ERROR: 3, logging.WARNING: 4, NOTICE: 5, logging.INFO: 6, logging.DEBUG: 7}


class SyslogPrefixFormatter(logging.Formatter):
    """Prefix every line with <N> so journald stores the right priority."""

    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record)
        level = max((lvl for lvl in _SYSLOG if lvl <= record.levelno), default=logging.DEBUG)
        prefix = f"<{_SYSLOG[level]}>"
        return "\n".join(prefix + line for line in text.splitlines() or [""])


def setup_logging(level: str) -> None:
    handler = logging.StreamHandler(sys.stdout)
    if os.environ.get("JOURNAL_STREAM"):  # journald adds timestamps itself
        handler.setFormatter(SyslogPrefixFormatter("%(name)s: %(message)s"))
    else:
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    logging.getLogger("telethon").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpx2").setLevel(logging.WARNING)


def notice(logger: logging.Logger, message: str, *args: object) -> None:
    logger.log(NOTICE, message, *args)
