"""`BarabotApi`: everything a user can do, for every interface.

Bot commands, the agent, MCP and the Mini App are adapters over this
class. Each authenticates the user its own way and passes the Telegram
user ID; every method acts only on that user's subscriptions, and someone
else's subscription looks the same as a missing one. Adapters catch
`ApiError` and show its message.

Saving a subscription only writes the database. Chats come in as
@usernames, t.me links or marked IDs and come back as a typed `ChatRef`
with a `ChatStatus`; adapters decide how to show them. A new chat starts
`pending`; the scraper resolves, joins and reports it later, so an
unknown chat shows up as `failed` in `list_subscriptions`, not as an
error here.

Partial updates use `UNSET` for "keep this field". `None` is a value only
for `author_not`, where it removes the mirror role.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path

from barabot.core.chats import ChatId, ChatRef, ChatStatus, Invite, Username
from barabot.core.unset import UNSET, Unset
from barabot.domains.bot import subscriptions
from barabot.domains.bot.subscriptions import SubscriptionChat, SubscriptionRow

__all__ = [
    "UNSET",
    "ApiError",
    "BarabotApi",
    "ChatId",
    "ChatRef",
    "ChatStatus",
    "InvalidRequest",
    "Invite",
    "SubscriptionChat",
    "SubscriptionNotFound",
    "SubscriptionRow",
    "Unset",
    "Username",
]


class ApiError(Exception):
    """A failure whose message is safe to show to the user."""


class InvalidRequest(ApiError):
    """Missing or malformed input, such as an empty query or a link that is not a chat."""


class SubscriptionNotFound(ApiError):
    def __init__(self, subscription_id: int) -> None:
        super().__init__(f"no subscription {subscription_id}")
        self.subscription_id = subscription_id


class BarabotApi:
    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path

    async def add_subscription(
        self,
        user_id: int,
        *,
        author: str,
        query: str,
        chats: Sequence[str],
        author_not: str | None = None,
    ) -> SubscriptionRow:
        """Create a subscription. `chats` are @usernames, t.me links or marked chat IDs."""
        with _invalid_request():
            sub_id = await asyncio.to_thread(
                subscriptions.add,
                self._db_path,
                user_id=user_id,
                author=author,
                author_not=author_not,
                query=query,
                chats=chats,
            )
        row = await asyncio.to_thread(subscriptions.get, self._db_path, sub_id, user_id=user_id)
        assert row is not None, "a subscription just created is active"
        return row

    async def list_subscriptions(self, user_id: int) -> list[SubscriptionRow]:
        """The user's active subscriptions, oldest first, with the status of each chat."""
        return await asyncio.to_thread(subscriptions.list_all, self._db_path, user_id=user_id, active_only=True)

    async def get_subscription(self, user_id: int, subscription_id: int) -> SubscriptionRow:
        row = await asyncio.to_thread(subscriptions.get, self._db_path, subscription_id, user_id=user_id)
        if row is None:
            raise SubscriptionNotFound(subscription_id)
        return row

    async def update_subscription(
        self,
        user_id: int,
        subscription_id: int,
        *,
        author: str | Unset = UNSET,
        author_not: str | None | Unset = UNSET,
        query: str | Unset = UNSET,
        chats: Sequence[str] | Unset = UNSET,
    ) -> SubscriptionRow:
        """Change the given fields; `chats` replaces the whole list.

        A changed author, author_not or query bumps `revision`; chats alone do not.
        """
        with _invalid_request():
            row = await asyncio.to_thread(
                subscriptions.update,
                self._db_path,
                subscription_id,
                user_id=user_id,
                author=author,
                author_not=author_not,
                query=query,
                chats=chats,
            )
        if row is None:
            raise SubscriptionNotFound(subscription_id)
        return row

    async def remove_subscription(self, user_id: int, subscription_id: int) -> None:
        removed = await asyncio.to_thread(subscriptions.deactivate, self._db_path, subscription_id, user_id=user_id)
        if not removed:
            raise SubscriptionNotFound(subscription_id)


@contextmanager
def _invalid_request() -> Iterator[None]:
    """Turn the storage layer's ValueError into InvalidRequest."""
    try:
        yield
    except ValueError as exc:
        raise InvalidRequest(str(exc)) from exc
