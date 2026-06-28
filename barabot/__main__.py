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

from barabot.core.config import Config
from barabot.core.locks import _acquire_lock, _release_lock, _remove_pid, _write_pid
from barabot.core.service import Service
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


def _load_config_or_exit() -> Config:
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
        sys.exit(2)


def _check_config() -> int:
    """`run --check`: validate .env, print summary, return exit code."""
    cfg = _load_config_or_exit()
    print(
        f"barabot config ok: api_id={cfg.api_id} "
        f"chats={len(cfg.watch_chats)} threshold={cfg.similarity_threshold} "
        f"log_level={cfg.log_level} db={cfg.db_path}"
    )
    return 0


def _build_services(cfg: Config) -> list[Service]:
    """Construct all services. Add a new domain by adding one line here."""
    return [
        ScraperService(cfg),
        ProcessorService(cfg, conn=None),
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
            "barabot up (skeleton): %d service(s), watching %d chat(s), db=%s",
            len(services),
            len(cfg.watch_chats),
            cfg.db_path,
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


def main() -> int:
    parser = argparse.ArgumentParser(prog="barabot")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_run = sub.add_parser("run", help="Run scraper + processor + bot services.")
    p_run.add_argument("--check", action="store_true")

    args = parser.parse_args()

    if args.cmd == "run":
        if args.check:
            return _check_config()
        asyncio.run(_run_barabot())
        return 0

    parser.error(f"unknown command: {args.cmd}")
    return 2  # unreachable


if __name__ == "__main__":
    sys.exit(main())