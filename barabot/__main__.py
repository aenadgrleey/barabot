"""Unified entry point: `python -m barabot run [--check]`.

Skeleton composer. Wires the live services (scraper, processor) into a
single `asyncio.TaskGroup` and shares one `asyncio.Event` for shutdown.

Lifecycle:
  - Load config (lazy import to keep `--help` fast and config errors
    specific).
  - Single-instance lock via `core.locks._acquire_lock` + PID file.
  - Spawn both services' `run(stop)` coroutines inside one TaskGroup —
    when `stop` fires, both wake up and exit their `try/finally`. The
    TaskGroup's `__aexit__` awaits them; any exception propagates.
  - SIGTERM/SIGINT sets `stop`. The second signal force-exits (an
    asyncio.TaskGroup can't be cancelled mid-cleanup cleanly, so
    `sys.exit` is the pragmatic backstop).

We do NOT use a custom `Service` Protocol — `asyncio.TaskGroup` already
gives us structured concurrency (cancel siblings on exception, await
all on exit) and an `asyncio.Event` is the right primitive for "stop".
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys

from barabot.core.locks import _acquire_lock, _release_lock, _remove_pid, _write_pid
from barabot.domains.processor import ProcessorService
from barabot.domains.scraper import ScraperService

log = logging.getLogger("barabot")


def _configure_logging(level: str) -> None:
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )


def _check_config() -> int:
    try:
        from barabot.core.config import load as barabot_load
    except Exception as exc:  # noqa: BLE001
        print(f"config check failed: {exc}", file=sys.stderr)
        return 2
    try:
        cfg = barabot_load()
    except Exception as exc:  # noqa: BLE001
        print(f"config check failed: {exc}", file=sys.stderr)
        return 2
    print(
        f"barabot config ok: api_id={cfg.api_id} "
        f"chats={len(cfg.watch_chats)} threshold={cfg.similarity_threshold} "
        f"log_level={cfg.log_level} db={cfg.db_path}"
    )
    return 0


async def _run_barabot() -> None:
    from barabot.core.config import load as barabot_load

    cfg = barabot_load()
    _configure_logging(cfg.log_level)

    lock_fd = _acquire_lock(cfg.lock_path)
    _write_pid(cfg.pid_path)
    stop = asyncio.Event()
    try:
        scraper = ScraperService(cfg)
        processor = ProcessorService(cfg, conn=None)

        # Wire SIGTERM/SIGINT → stop. The second signal force-exits so a
        # wedged service can't hold us forever.
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
            "barabot up (skeleton): scraper + processor stubs, "
            "watching %d chat(s), db=%s",
            len(cfg.watch_chats),
            cfg.db_path,
        )
        async with asyncio.TaskGroup() as tg:
            tg.create_task(scraper.run(stop), name="scraper")
            tg.create_task(processor.run(stop), name="processor")
    finally:
        _remove_pid(cfg.pid_path)
        _release_lock(lock_fd, cfg.lock_path)
    log.info("barabot stopped cleanly")


def main() -> int:
    parser = argparse.ArgumentParser(prog="barabot")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_run = sub.add_parser("run", help="Run the scraper + processor services.")
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