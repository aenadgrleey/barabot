"""Unified entry point: `python -m barabot run [--check]`.

Composer. Starts DBOS, then wires the runtime services (scraper, bot)
into a single `asyncio.TaskGroup` with one shared shutdown `asyncio.Event`.
Every service follows the contract in `barabot.core.service.Service`:

    async start()              # open resources, attach handlers
    async run_forever(stop)    # block until stop fires
    async stop()               # symmetric teardown

Processing is not a service. It runs as DBOS workflows, and services
meet it only through DBOS and the application database.

Lifecycle:
  - Validate config FIRST via `_load_config_or_exit()`: bad .env fails
    with a clear message and exit 2, before we touch asyncio.
  - Single-instance lock via `core.locks._acquire_lock` + PID file.
  - Migrate the app database, construct the processor, launch DBOS,
    open the processor's queue. The processor exists before launch so
    DBOS can recover its runs; the scraper may enqueue from its first
    event, so all of this precedes the services.
  - Construct all services, then `start()` each before any can race.
    If any start() raises, we never run.
  - Inside one TaskGroup, spawn each service's `run_forever(stop)`.
    On the first exception the group cancels siblings: a torn
    half-process is worse than a clean exit.
  - SIGTERM/SIGINT sets `stop`. The second signal force-exits.
  - Finally: started services stop in reverse order, then DBOS shuts
    down, regardless of how we exited.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
from collections.abc import Sequence
from pathlib import Path

from telethon import TelegramClient

from barabot.core import db, durable
from barabot.core.config import Config
from barabot.core.locks import _acquire_lock, _release_lock, _remove_pid, _write_pid
from barabot.core.service import Service
from barabot.core.setup_hints import (
    PROCESSOR_COMMAND,
    PROJECT_ROOT,
    SCRAPER_COMMAND,
    SETUP_COMMAND,
    missing_setup_commands,
    next_setup_command,
    setup_guidance_commands,
)
from barabot.core.telegram import TelegramConfigError
from barabot.domains.bot.client import TelethonSender, make_bot_client
from barabot.domains.bot.notifier import Notifier
from barabot.domains.bot.service import BotService
from barabot.domains.processor.budget import Budget
from barabot.domains.processor.listing import ListingProcessor
from barabot.domains.processor.matching import JEV_IMAGES, JevDecider
from barabot.domains.processor.stats import StatsReporter
from barabot.domains.scraper import ScraperService
from barabot.domains.scraper.ingest import MessageIngest
from barabot.domains.scraper.photos import PhotoDownloader

log = logging.getLogger("barabot")


def _configure_logging(level: str) -> None:
    from barabot.core.logs import setup_logging

    setup_logging(level)


def _load_config_or_exit(*, setup_root: Path | None = None) -> Config:
    """Load + validate config, or print+exit on failure.

    Single source of truth for the .env read. Both `--check` and the
    bare `run` path funnel through here so they share one parser and
    one set of error messages.
    """
    from barabot.core.config import load as barabot_load

    try:
        return barabot_load()
    except TelegramConfigError as exc:
        print(f"config check failed: {exc}", file=sys.stderr)
        sys.exit(2)
    except Exception as exc:
        print(f"config check failed: {exc}", file=sys.stderr)
        commands = setup_guidance_commands(setup_root or PROJECT_ROOT)
        if commands:
            print("missing setup:", file=sys.stderr)
            for command in commands:
                print(f"  {command}", file=sys.stderr)
        sys.exit(2)


def _check_config() -> int:
    """`run --check`: validate .env, print summary, return exit code."""
    cfg = _load_config_or_exit()
    pending = missing_setup_commands(PROJECT_ROOT, cfg.telegram)
    if pending:
        print("config check failed: setup is incomplete", file=sys.stderr)
        print("missing setup:", file=sys.stderr)
        for command in dict.fromkeys([SETUP_COMMAND, *pending]):
            print(f"  {command}", file=sys.stderr)
        return 2
    print(f"barabot config ok: scraper={cfg.telegram.backend} chats={len(cfg.watch_chats)} log_level={cfg.log_level}")
    return 0


def _build_matching(cfg: Config, budget: Budget) -> tuple[JevDecider, PhotoDownloader]:
    """The decider and the photo downloader that suits it are chosen together.

    Switching models means changing this pair: the photo downloader gets
    the decider's ImageSpec here, so the scraper never sees the decider.
    """
    from typesafe_sdk import AsyncTypeSafeClient

    decider = JevDecider(AsyncTypeSafeClient(api_key=cfg.typesafe_api_key), budget=budget)
    return decider, PhotoDownloader(JEV_IMAGES, cfg.media_dir)


def _build_services(
    cfg: Config,
    processor: ListingProcessor,
    photos: PhotoDownloader,
    budget: Budget,
    bot_client: TelegramClient,
) -> list[Service]:
    """Construct all services. Add a new domain by adding one line here."""
    services: list[Service] = [
        ScraperService(cfg, MessageIngest(cfg.db_path, processor.schedule), photos),
        BotService(cfg, bot_client),
    ]
    if cfg.stats_interval_minutes:
        services.append(StatsReporter(cfg.db_path, budget, interval_seconds=cfg.stats_interval_minutes * 60))
    return services


def _announce_start(cfg: Config, budget: Budget) -> None:
    from barabot.core.db import connect
    from barabot.core.logs import notice

    conn = connect(cfg.db_path)
    try:
        active = conn.execute("SELECT count(*) FROM subscriptions WHERE active = 1").fetchone()[0]
    finally:
        conn.close()
    limit = f"${budget.limit_usd:.2f}" if budget.limit_usd else "no limit"
    notice(
        log,
        "barabot up: watching %d chat(s), %d active subscription(s), Jev spend $%.3f of %s",
        len(cfg.watch_chats),
        active,
        budget.spent_usd(),
        limit,
    )


async def _run_barabot() -> None:
    # Validate config BEFORE spinning up the event loop and TaskGroup.
    # A bad .env should fail with a clear message, not crash inside
    # Telethon or processing code 5s into startup. _load_config_or_exit() prints
    # the specific reason and exits non-zero.
    cfg = _load_config_or_exit()
    _configure_logging(cfg.log_level)

    lock_fd = _acquire_lock(cfg.lock_path)
    _write_pid(cfg.pid_path)
    stop = asyncio.Event()
    services: list[Service] = []
    dbos_up = False
    decider: JevDecider | None = None
    try:
        db.migrate(cfg.db_path)
        budget = Budget(cfg.db_path, limit_usd=cfg.jev_budget_usd, price_per_m_input=cfg.jev_price_per_m_input)
        decider, photos = _build_matching(cfg, budget)
        # The bot client exists, unconnected, before anything that uses it:
        # BotService connects it, Notifier sends through it.
        bot_client = make_bot_client(cfg)
        # Before launch: DBOS finds the instances by name when it recovers runs.
        notifier = Notifier(cfg.db_path, TelethonSender(bot_client))
        processor = ListingProcessor(cfg.db_path, cfg.media_dir, decider, notifier)
        durable.launch(cfg.dbos_path)
        dbos_up = True
        await processor.open()
        await notifier.open()
        services = _build_services(cfg, processor, photos, budget, bot_client)

        # Start every service before any can race. If one raises,
        # we tear down the already-started ones in reverse order.
        for svc in services:
            await svc.start()

        # Wire SIGTERM/SIGINT → stop. The second signal force-exits so
        # a wedged service can't hold us forever.
        loop = asyncio.get_running_loop()
        force_stop = False

        def _request_stop(sig: signal.Signals) -> None:
            nonlocal force_stop
            if force_stop:
                log.warning("received %s again, force-exiting", sig.name)
                sys.exit(1)
            force_stop = True
            log.info("received %s, shutting down", sig.name)
            stop.set()

        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, _request_stop, sig)

        _announce_start(cfg, budget)
        async with asyncio.TaskGroup() as tg:
            for svc in services:
                tg.create_task(svc.run_forever(stop), name=type(svc).__name__)
    finally:
        # Stop in reverse order, last started first: the bot drops before
        # the scraper disconnects from Telegram. DBOS goes down after both.
        for svc in reversed(services):
            try:
                await svc.stop()
            except Exception as exc:  # best-effort cleanup
                log.error("error stopping %s: %s", type(svc).__name__, exc)
        if dbos_up:
            durable.shutdown()
        if decider is not None:
            await decider.aclose()
        _remove_pid(cfg.pid_path)
        _release_lock(lock_fd, cfg.lock_path)
    log.info("barabot stopped cleanly")


def _chat_arg(raw: str) -> str | int:
    try:
        return int(raw)
    except ValueError:
        return raw


def _subscription_command(args: argparse.Namespace) -> int:
    from barabot.core.config import DATA_DIR
    from barabot.domains.bot import subscriptions

    path = DATA_DIR / "barabot.sqlite"
    db.migrate(path)
    if args.subscription_cmd == "add":
        try:
            sub_id = subscriptions.add(
                path,
                user_id=args.user,
                author=args.author,
                author_not=args.author_not,
                query=args.query,
                chats=args.chat,
            )
        except ValueError as exc:
            print(f"subscription not created: {exc}", file=sys.stderr)
            return 2
        print(f"subscription {sub_id} created")
        return 0
    if args.subscription_cmd == "list":
        for row in subscriptions.list_all(path):
            state = "active" if row.active else "off"
            not_part = f" | not: {row.author_not}" if row.author_not else ""
            chats = ",".join(f"{c.ref}({c.status})" for c in row.chats)
            print(
                f"{row.id:>4} {state:6} user={row.user_id} chats={chats}\n"
                f"     author: {row.author}{not_part}\n     query: {row.query}"
            )
        return 0
    if subscriptions.deactivate(path, args.id):
        print(f"subscription {args.id} deactivated")
        return 0
    print(f"no active subscription {args.id}", file=sys.stderr)
    return 1


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="barabot")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_run = sub.add_parser("run", help="Run the scraper and bot services with DBOS processing.")
    p_run.add_argument("--check", action="store_true")

    p_setup = sub.add_parser("setup", help="Configure all missing components.")
    p_setup.add_argument("-f", "--force", "--forced", action="store_true", help="Reconfigure components.")
    p_setup.add_argument(
        "--headless",
        action="store_true",
        help="Configure from environment variables without prompting.",
    )
    setup_sub = p_setup.add_subparsers(dest="setup_component")
    setup_sub.add_parser("scraper", help="Authorize the Telegram session with OTP/2FA.")

    p_processor = sub.add_parser("processor", help="Manage the processor.")
    processor_sub = p_processor.add_subparsers(dest="processor_cmd", required=True)
    processor_sub.add_parser("configure", help="Store the TypeSafe API key used to match subscriptions.")
    processor_sub.add_parser(
        "redecide", help="Mark every listing undecided; restart barabot and backfill re-checks recent ones."
    )

    p_scraper = sub.add_parser("scraper", help="One-off scraper commands. Stop barabot.service first.")
    scraper_sub = p_scraper.add_subparsers(dest="scraper_cmd", required=True)
    p_join = scraper_sub.add_parser("join", help="Join chats and print their IDs for WATCH_CHATS.")
    p_join.add_argument("targets", nargs="+", help="@username or t.me link")
    p_sample = scraper_sub.add_parser("sample", help="Dump recent messages of a chat as JSON lines.")
    p_sample.add_argument("chat", help="@username or chat ID")
    p_sample.add_argument("--limit", type=int, default=200)
    p_sample.add_argument("--out", type=Path, required=True)

    p_subs = sub.add_parser("subscription", help="Manage subscriptions until the bot does.")
    subs_sub = p_subs.add_subparsers(dest="subscription_cmd", required=True)
    p_add = subs_sub.add_parser("add", help="Create a subscription.")
    p_add.add_argument("--user", type=int, required=True, help="Telegram user ID to notify")
    p_add.add_argument(
        "--chat", action="append", required=True, help="@username, t.me link or marked chat ID; repeat for several"
    )
    p_add.add_argument("--author", required=True, help="What the author of a wanted message does")
    p_add.add_argument("--author-not", help="Mirror role that must not match")
    p_add.add_argument("--query", required=True, help="What the message is about")
    subs_sub.add_parser("list", help="List subscriptions.")
    p_remove = subs_sub.add_parser("remove", help="Deactivate a subscription.")
    p_remove.add_argument("id", type=int)

    p_bot = sub.add_parser("bot", help="Manage the Telegram bot.")
    bot_sub = p_bot.add_subparsers(dest="bot_cmd", required=True)
    p_bot_auth = bot_sub.add_parser("auth", help="Verify and store a BotFather token.")
    p_bot_auth.add_argument("token", help="BotFather token")

    args = parser.parse_args(argv)

    if args.cmd == "run":
        if args.check:
            return _check_config()
        asyncio.run(_run_barabot())
        return 0

    if args.cmd == "setup":
        try:
            if args.setup_component == "scraper":
                from barabot.core.setup import setup_scraper

                return asyncio.run(setup_scraper(root=PROJECT_ROOT))
            from barabot.core.setup import setup

            return asyncio.run(setup(root=PROJECT_ROOT, force=args.force, headless=args.headless))
        except TelegramConfigError as exc:
            parser.error(str(exc))
        except (KeyboardInterrupt, EOFError):
            command = SCRAPER_COMMAND if args.setup_component == "scraper" else SETUP_COMMAND
            print(f"Cancelled; rerun with:\n{command}", file=sys.stderr)
            return 130

    if args.cmd == "processor" and args.processor_cmd == "configure":
        from barabot.domains.processor.configure import ConfigurationError, configure

        try:
            saved_path = configure()
        except (KeyboardInterrupt, EOFError):
            print(f"Cancelled; rerun with:\n{PROCESSOR_COMMAND}", file=sys.stderr)
            return 130
        except ConfigurationError:
            print("processor configuration failed", file=sys.stderr)
            return 1
        print(f"Processor configuration saved: {saved_path}")
        expected_path = PROJECT_ROOT / "config" / "processor.env"
        if saved_path == expected_path:
            print(f"Next: {next_setup_command(PROJECT_ROOT)}")
        return 0

    if args.cmd == "subscription":
        return _subscription_command(args)

    if args.cmd == "processor" and args.processor_cmd == "redecide":
        from barabot.core.config import DATA_DIR
        from barabot.core.db import connect

        path = DATA_DIR / "barabot.sqlite"
        db.migrate(path)
        conn = connect(path)
        try:
            count = conn.execute("UPDATE listings SET decided_version = 0 WHERE decided_version > 0").rowcount
        finally:
            conn.close()
        print(f"{count} listing(s) marked undecided. Restart barabot: backfill re-checks the last BACKFILL_HOURS.")
        return 0

    if args.cmd == "scraper":
        from barabot.domains.scraper import admin

        cfg = _load_config_or_exit()
        _configure_logging("WARNING")
        if args.scraper_cmd == "join":
            joined = asyncio.run(admin.join(cfg, args.targets))
            for chat in joined:
                print(f"{chat['id']}  {chat['kind']:16}  {chat['status']:16}  {chat['title']}  ({chat['target']})")
            print("WATCH_CHATS=" + ",".join(str(chat["id"]) for chat in joined))
            return 0
        count = asyncio.run(admin.sample(cfg, _chat_arg(args.chat), args.limit, args.out))
        print(f"{count} message(s) written to {args.out}")
        return 0

    if args.cmd == "bot" and args.bot_cmd == "auth":
        from barabot.domains.bot.auth import AuthorizationError, authorize

        try:
            identity = authorize(args.token, Config.bot_token_path)
        except AuthorizationError:
            print("bot authorization failed", file=sys.stderr)
            return 1
        print(
            f"Telegram bot authorized: id={identity.id} "
            f"username=@{identity.username} token_file={Config.bot_token_path}"
        )
        print(f"Next: {next_setup_command(PROJECT_ROOT)}")
        return 0

    parser.error(f"unknown command: {args.cmd}")
    return 2  # unreachable


if __name__ == "__main__":
    sys.exit(main())
