"""Unified entry point: `python -m barabot` or `python -m barabot reporter`."""
from __future__ import annotations

import argparse
import asyncio
import sys

from .bot import run as barabot_run
from .config import load as barabot_load


def _barabot_check() -> int:
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


def main() -> int:
    parser = argparse.ArgumentParser(prog="barabot")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_run = sub.add_parser("run", help="Run the userbot (barabot).")
    p_run.add_argument("--check", action="store_true")

    p_rep = sub.add_parser("reporter", help="Run the reporter bot (bot API).")
    p_rep.add_argument("--check", action="store_true")

    args = parser.parse_args()

    if args.cmd == "run":
        if args.check:
            return _barabot_check()
        asyncio.run(barabot_run())
        return 0

    if args.cmd == "reporter":
        # Lazy import — keeps reporter deps off the barabot-only startup path
        # and avoids importing pyrogram (bot API path) unless needed.
        # `reporter` is a sibling package next to `barabot/`, not a subpackage.
        from reporter import __main__ as rep_main
        sys.argv = ["barabot-reporter", *(["--check"] if args.check else [])]
        return rep_main.main()

    parser.error(f"unknown command: {args.cmd}")
    return 2  # unreachable


if __name__ == "__main__":
    sys.exit(main())
