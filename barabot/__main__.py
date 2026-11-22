"""Unified entry point: `python -m barabot run [--check]`.

Composer. Wires the three runtime services (scraper, processor, bot)
into a single `asyncio.TaskGroup` and shares one `asyncio.Event` for
shutdown. Every service follows the same contract:

    async start()              # open resources, attach handlers
    async run_forever(stop)    # block until stop fires
    async stop()               # symmetric teardown

defined in `barabot.core.service.Service`. The composer is
domain-agnostic — it doesn't know what scraper/processor/bot do, just
that they obey the lifecycle. Adding a new domain is one line in the
SERVICES tuple below and one import.

Lifecycle:
  - Validate config FIRST via `_load_config_or_exit()` — bad .env fails
    here with a clear message and exit 2, before we touch asyncio.
  - Single-instance lock via `core.locks._acquire_lock` + PID file.
  - Construct all services, then `start()` each (synchronously, before
    any of them can race). If any start() raises, we never run.
  - Inside one TaskGroup, spawn each service's `run_forever(stop)`
    coroutine. The TaskGroup awaits all; on the first exception it
    cancels siblings (so a crash in scraper pulls processor and bot
    down too — desirable: a torn half-process is worse than a clean
    exit).
  - SIGTERM/SIGINT sets `stop`. The second signal force-exits (a
    TaskGroup can't be cancelled mid-cleanup cleanly, so `sys.exit`
    is the pragmatic backstop).
  - Finally: every started service's `stop()` runs, in reverse order,
    regardless of whether we exited cleanly or by exception.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
from collections.abc import Sequence
from pathlib import Path

from barabot.core.config import Config
from barabot.core.locks import _acquire_lock, _release_lock, _remove_pid, _write_pid
from barabot.core.service import Service
from barabot.core.setup_hints import (
    PROCESSOR_COMMAND,
    PROJECT_ROOT,
    READY_COMMAND,
    SCRAPER_COMMAND,
    SETUP_COMMAND,
    missing_setup_commands,
    next_setup_command,
    setup_guidance_commands,
)
from barabot.domains.bot import BotService
from barabot.domains.processor import ProcessorService
from barabot.domains.scraper import ScraperService

log = logging.getLogger("barabot")


def _configure_logging(level: str) -> None:
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )


def _load_config_or_exit(*, setup_root: Path | None = None) -> Config:
    """Load + validate config, or print+exit on failure.

    Single source of truth for the .env read. Both `--check` and the
    bare `run` path funnel through here so they share one parser and
    one set of error messages.
    """
    from barabot.core.config import load as barabot_load

    try:
        return barabot_load()
    except Exception as exc:  # noqa: BLE001
        print(f"config check failed: {exc}", file=sys.stderr)
        commands = setup_guidance_commands(setup_root or PROJECT_ROOT)
        if commands:
            print("missing setup:", file=sys.stderr)
            for command in commands:
                print(f"  {command}", file=sys.stderr)
        sys.exit(2)


def _check_config() -> int:
    """`run --check`: validate .env, print summary, return exit code."""
    pending = missing_setup_commands(PROJECT_ROOT)
    if pending:
        print("config check failed: setup is incomplete", file=sys.stderr)
        print("missing setup:", file=sys.stderr)
        for command in setup_guidance_commands(PROJECT_ROOT):
            print(f"  {command}", file=sys.stderr)
        return 2
    cfg = _load_config_or_exit()
    print(
        f"barabot config ok: api_id={cfg.api_id} "
        f"chats={len(cfg.watch_chats)} threshold={cfg.similarity_threshold} "
        f"log_level={cfg.log_level}"
    )
    return 0


def _build_services(cfg: Config) -> list[Service]:
    """Construct all services. Add a new domain by adding one line here."""
    return [
        ScraperService(cfg),
        ProcessorService(cfg),
        BotService(cfg, client=None),
    ]


async def _run_barabot() -> None:
    # Validate config BEFORE spinning up the event loop and TaskGroup.
    # A bad .env should fail with a clear message, not crash inside
    # Telethon/embed code 5s into startup. _load_config_or_exit() prints
    # the specific reason and exits non-zero.
    cfg = _load_config_or_exit()
    _configure_logging(cfg.log_level)

    lock_fd = _acquire_lock(cfg.lock_path)
    _write_pid(cfg.pid_path)
    stop = asyncio.Event()
    services: list[Service] = []
    try:
        services = _build_services(cfg)

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

        log.info(
            "barabot up (skeleton): %d service(s), watching %d chat(s)",
            len(services),
            len(cfg.watch_chats),
        )
        async with asyncio.TaskGroup() as tg:
            for svc in services:
                tg.create_task(svc.run_forever(stop), name=type(svc).__name__)
    finally:
        # Stop in reverse order — last started, first stopped — so the
        # bot drops before the processor stops accepting work, and the
        # processor stops before the scraper disconnects from Telegram.
        for svc in reversed(services):
            try:
                await svc.stop()
            except Exception as exc:  # noqa: BLE001 — best-effort cleanup
                log.error("error stopping %s: %s", type(svc).__name__, exc)
        _remove_pid(cfg.pid_path)
        _release_lock(lock_fd, cfg.lock_path)
    log.info("barabot stopped cleanly")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="barabot")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_run = sub.add_parser("run", help="Run scraper + processor + bot services.")
    p_run.add_argument("--check", action="store_true")

    p_setup = sub.add_parser("setup", help="Configure all missing components.")
    p_setup.add_argument(
        "-f", "--force", "--forced", action="store_true", help="Reconfigure components."
    )
    p_setup.add_argument(
        "--headless",
        action="store_true",
        help="Configure from environment variables without prompting.",
    )
    setup_sub = p_setup.add_subparsers(dest="setup_component")
    setup_sub.add_parser("scraper", help="Authorize the Telegram session with OTP/2FA.")

    p_processor = sub.add_parser("processor", help="Manage the processor.")
    processor_sub = p_processor.add_subparsers(dest="processor_cmd", required=True)
    processor_sub.add_parser("configure", help="Configure embedding provider settings.")

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

            return asyncio.run(
                setup(root=PROJECT_ROOT, force=args.force, headless=args.headless)
            )
        except (KeyboardInterrupt, EOFError):
            command = (
                SCRAPER_COMMAND if args.setup_component == "scraper" else SETUP_COMMAND
            )
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
