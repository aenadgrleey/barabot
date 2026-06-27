"""The reporter bot: pyrogram (Bot API), commands, polling for new items."""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import sys

from pyrogram import Client, filters
from pyrogram.handlers import MessageHandler
from pyrogram.types import Message

from .config import Config, load
from .db import (
    chat_counts, connect_ro, fetch_recent, fetch_since, get_item,
    max_rowid, search,
)
from .formatting import HELP, format_chat_counts, format_item, format_list, format_new
from barabot.locks import _acquire_lock, _release_lock, _remove_pid, _write_pid

log = logging.getLogger("barabot.reporter")


def _is_allowed(cfg: Config, message: Message) -> bool:
    """Empty allowed list means 'no one can talk to me'. Non-empty = whitelist."""
    if not cfg.allowed_chat_ids:
        return False
    return message.chat.id in cfg.allowed_chat_ids


def _parse_command_args(message: Message) -> list[str]:
    """Strip the leading /command and split on whitespace."""
    text = (message.text or "").strip()
    if not text.startswith("/"):
        return []
    parts = text.split(maxsplit=1)
    cmd = parts[0].split("@", 1)[0]  # drop @botname suffix
    rest = parts[1] if len(parts) > 1 else ""
    return [cmd, *rest.split()]


def make_client(cfg: Config) -> Client:
    return Client(
        name="barabot_reporter",
        api_id=cfg.api_id,
        api_hash=cfg.api_hash,
        bot_token=cfg.bot_token,
        in_memory=False,
    )


class Reporter:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.conn = connect_ro(cfg.db_path)
        self.app = make_client(cfg)
        self._stopping = asyncio.Event()
        self._last_rowid: int = max_rowid(self.conn)
        log.info("reporter db ready, starting from rowid=%d", self._last_rowid)

    # ---- signal handling -------------------------------------------------

    def install_signal_handlers(self, loop: asyncio.AbstractEventLoop) -> None:
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, self._on_signal, sig)

    def _on_signal(self, sig: signal.Signals) -> None:
        log.info("received %s, shutting down", sig.name)
        self._stopping.set()

    # ---- command handlers ------------------------------------------------

    async def on_command(self, _client, message: Message) -> None:
        if not self._is_authorized(message):
            log.warning("unauthorized chat %s tried to command me", message.chat.id)
            return
        parts = _parse_command_args(message)
        if not parts:
            return
        cmd, *args = parts
        try:
            if cmd == "/start":
                await message.reply_text(HELP)
            elif cmd == "/help":
                await message.reply_text(HELP)
            elif cmd == "/latest":
                n = int(args[0]) if args else self.cfg.default_limit
                n = max(1, min(n, 100))
                items = fetch_recent(self.conn, n)
                await message.reply_text(
                    format_list(items, f"Last {n} items (newest first):"),
                    parse_mode=None,
                )
            elif cmd == "/chats":
                counts = chat_counts(self.conn)
                await message.reply_text(
                    format_chat_counts(counts), parse_mode=None,
                )
            elif cmd == "/search":
                if not args:
                    await message.reply_text("usage: /search <text>")
                    return
                needle = " ".join(args)
                items = search(self.conn, needle)
                await message.reply_text(
                    format_list(items, f"Search: {needle!r} ({len(items)} hits)"),
                    parse_mode=None,
                )
            elif cmd == "/get":
                if len(args) != 2:
                    await message.reply_text("usage: /get <chat_id> <msg_id>")
                    return
                try:
                    chat_id = int(args[0])
                    msg_id = int(args[1])
                except ValueError:
                    await message.reply_text("chat_id and msg_id must be integers")
                    return
                item = get_item(self.conn, chat_id, msg_id)
                if not item:
                    await message.reply_text("not found")
                    return
                await message.reply_text(format_item(item), parse_mode=None)
            else:
                await message.reply_text(f"unknown command: {cmd}\n\n{HELP}")
        except Exception as exc:  # noqa: BLE001
            log.exception("command %s failed", cmd)
            await message.reply_text(f"error: {exc}")

    def _is_authorized(self, message: Message) -> bool:
        return _is_allowed(self.cfg, message)

    # ---- new-item poller --------------------------------------------------

    async def poll_new_items(self) -> None:
        """Background task: every REPORTER_POLL_INTERVAL seconds, look for
        new rows in barabot's items table and broadcast them."""
        targets = self.cfg.allowed_chat_ids
        if not targets:
            log.warning("ALLOWED_CHAT_IDS is empty — I won't push anything")
        while not self._stopping.is_set():
            try:
                new = fetch_since(self.conn, self._last_rowid)
            except Exception as exc:  # noqa: BLE001
                log.error("db read failed: %s", exc)
                await asyncio.sleep(self.cfg.poll_interval * 2)
                continue
            for it in new:
                self._last_rowid = max(self._last_rowid, it["rowid"])
                if not targets:
                    log.info("(would push) %s", format_new(it))
                    continue
                for tid in targets:
                    try:
                        await self.app.send_message(tid, format_new(it), parse_mode=None)
                    except Exception as exc:  # noqa: BLE001
                        log.error("push to %s failed: %s", tid, exc)
            await asyncio.sleep(self.cfg.poll_interval)

    # ---- handler wiring ---------------------------------------------------

    def attach_handlers(self) -> None:
        if not self.cfg.allowed_chat_ids:
            # No whitelist yet — skip command wiring entirely. Operator can
            # fill ALLOWED_CHAT_IDS and restart to enable.
            log.warning("ALLOWED_CHAT_IDS empty — command handlers disabled")
            return
        chat_filter = filters.chat(self.cfg.allowed_chat_ids)
        self.app.add_handler(
            MessageHandler(self.on_command, filters.command(["start", "help", "latest", "chats", "search", "get"]) & chat_filter),
            group=0,
        )

    # ---- lifecycle --------------------------------------------------------

    async def run(self) -> None:
        self.attach_handlers()
        log.info(
            "reporter up: watching db=%s, targets=%s, poll=%.1fs",
            self.cfg.db_path, self.cfg.allowed_chat_ids, self.cfg.poll_interval,
        )
        poll_task = asyncio.create_task(self.poll_new_items())
        try:
            await self._stopping.wait()
        finally:
            poll_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await poll_task


def _configure_logging(level: str) -> None:
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )
    logging.getLogger("pyrogram").setLevel(logging.WARNING)


async def run() -> None:
    cfg = load()
    _configure_logging(cfg.log_level)

    lock_fd = _acquire_lock(cfg.lock_path)
    _write_pid(cfg.pid_path)
    try:
        reporter = Reporter(cfg)
        loop = asyncio.get_running_loop()
        reporter.install_signal_handlers(loop)
        try:
            await reporter.app.start()
            log.info(
                "barabot reporter up: db=%s, targets=%s, pid=%d",
                cfg.db_path, cfg.allowed_chat_ids, os.getpid(),
            )
            await reporter.run()
        finally:
            with contextlib.suppress(Exception):
                if reporter.app.is_initialized:
                    await reporter.app.stop()
    finally:
        _remove_pid(cfg.pid_path)
        _release_lock(lock_fd, cfg.lock_path)
    log.info("barabot reporter stopped cleanly")
