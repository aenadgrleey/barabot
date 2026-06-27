"""python -m reporter — entry point. Supports --check for config validation."""
from __future__ import annotations

import argparse
import asyncio
import sys

from .bot import run
from .config import load


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="barabot-reporter",
        description="Bot API companion that pushes 'NEW item' notifications and answers /commands.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Validate config and .env, then exit 0.",
    )
    args = parser.parse_args()

    if args.check:
        try:
            cfg = load()
        except Exception as exc:  # noqa: BLE001
            print(f"config check failed: {exc}", file=sys.stderr)
            return 2
        print(
            f"config ok: bot_token=…{cfg.bot_token[-6:] if len(cfg.bot_token) >= 6 else '???'} "
            f"targets={cfg.allowed_chat_ids} poll={cfg.poll_interval}s db={cfg.db_path}"
        )
        return 0

    asyncio.run(run())
    return 0


if __name__ == "__main__":
    sys.exit(main())
