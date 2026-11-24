"""The bot's Telegram connection: a plain Telethon client logged in with the BotFather token.

It always talks to Telegram directly, never through tgmux, which carries
user sessions only. Telethon needs API_ID and API_HASH for that, from
config/telegram.env, the same application a local scraper session uses.
"""

from __future__ import annotations

from telethon import TelegramClient, errors

from barabot.core.config import Config
from barabot.domains.bot.notifier import Undeliverable

# Telegram answers these when the user cannot get the message at all.
_UNREACHABLE = (
    errors.UserIsBlockedError,
    errors.InputUserDeactivatedError,
    errors.PeerIdInvalidError,
    errors.MessageIdInvalidError,
)


def make_bot_client(cfg: Config) -> TelegramClient:
    """Not connected yet; BotService connects it."""
    # catch_up: on connect, fetch updates sent while barabot was down, such as a /start.
    return TelegramClient(str(cfg.bot_session_path), cfg.api_id, cfg.api_hash, catch_up=True)


class TelethonSender:
    """`BotSender` over the bot's client. Plain text; the preview of the post link shows the listing."""

    def __init__(self, client: TelegramClient) -> None:
        self._client = client

    async def send(self, user_id: int, text: str) -> int:
        try:
            message = await self._client.send_message(user_id, text, parse_mode=None, link_preview=True)
        except _UNREACHABLE as exc:
            raise Undeliverable(type(exc).__name__) from exc
        except ValueError as exc:  # Telethon: the user never wrote to the bot, so it has no access hash
            raise Undeliverable(str(exc)) from exc
        return message.id

    async def edit(self, user_id: int, message_id: int, text: str) -> None:
        try:
            await self._client.edit_message(user_id, message_id, text, parse_mode=None, link_preview=True)
        except errors.MessageNotModifiedError:
            return
        except _UNREACHABLE as exc:
            raise Undeliverable(type(exc).__name__) from exc
        except ValueError as exc:
            raise Undeliverable(str(exc)) from exc
