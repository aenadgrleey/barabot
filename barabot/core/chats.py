"""How a user names a chat, and where the scraper is with joining it.

`ChatRef` is one of three kinds, each joined differently by the scraper:

    Username  @name or t.me/name    resolve the username, then join
    Invite    t.me/+hash            import the invite
    ChatId    a marked ID, -100...  already resolved; check membership

The bot domain parses user input into a `ChatRef` and stores it in
`chats` as `kind` and `value`; the scraper reads it back with
`ref_from_row`. Strings with prefixes exist only at the edges: user
input and `str()` for display.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

_USERNAME = re.compile(r"[A-Za-z][A-Za-z0-9_]{3,31}")
_INVITE = re.compile(r"[A-Za-z0-9_-]+")
_LINK = re.compile(r"(?:https?://)?(?:t|telegram)\.me/(.+)", re.IGNORECASE)


class ChatStatus(StrEnum):
    PENDING = "pending"  # not joined yet
    JOINED = "joined"  # the scraper account is a member
    REQUESTED = "requested"  # waiting for an admin to approve the join request
    FAILED = "failed"  # the chat does not exist or joining failed


@dataclass(frozen=True)
class Username:
    name: str  # lower case, without @

    def __str__(self) -> str:
        return f"@{self.name}"


@dataclass(frozen=True)
class Invite:
    hash: str  # case-sensitive

    def __str__(self) -> str:
        return f"https://t.me/+{self.hash}"


@dataclass(frozen=True)
class ChatId:
    id: int  # marked: negative for groups and channels

    def __str__(self) -> str:
        return str(self.id)


ChatRef = Username | Invite | ChatId


def parse_chat(raw: str | int) -> ChatRef:
    """Accept @username, a t.me link to a chat, post or invite, or a marked chat ID."""
    if isinstance(raw, int):
        return _chat_id(raw, raw)
    text = raw.strip()
    if re.fullmatch(r"-?\d+", text):
        return _chat_id(int(text), raw)
    link = _LINK.fullmatch(text)
    if link:
        path = link.group(1).strip("/")
        invite = None
        if path.startswith("+"):
            invite = path[1:]
        elif path.startswith("joinchat/"):
            invite = path.removeprefix("joinchat/")
        if invite is not None:
            if _INVITE.fullmatch(invite):
                return Invite(invite)
            raise ValueError(f"not a Telegram invite link: {raw}")
        # t.me/s/name is the web preview; t.me/name/123 is a post in that chat.
        text = path.removeprefix("s/").split("/")[0]
    name = text.removeprefix("@")
    if _USERNAME.fullmatch(name):
        return Username(name.lower())
    raise ValueError(f"not a Telegram chat: {raw}")


def _chat_id(chat_id: int, raw: str | int) -> ChatId:
    # Positive IDs are users and bots, not chats with listings.
    if chat_id >= 0:
        raise ValueError(f"not a group or channel ID: {raw}")
    return ChatId(chat_id)


def ref_to_row(ref: ChatRef) -> tuple[str, str]:
    """`chats.kind` and `chats.value` for a reference."""
    match ref:
        case Username(name):
            return "username", name
        case Invite(hash_):
            return "invite", hash_
        case ChatId(id_):
            return "id", str(id_)


def ref_from_row(kind: str, value: str) -> ChatRef:
    match kind:
        case "username":
            return Username(value)
        case "invite":
            return Invite(value)
        case "id":
            return ChatId(int(value))
    raise ValueError(f"unknown chat kind {kind!r}")
