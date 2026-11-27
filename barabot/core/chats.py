"""How a user names a chat, and where the scraper is with joining it.

`ChatRef` is one of four kinds, each joined differently by the scraper:

    Username  @name or t.me/name    resolve the username, then join
    Invite    t.me/+hash            import the invite
    ChatId    a marked ID, -100...  already resolved; check membership
    ChatList  t.me/addlist/slug     a shared folder; resolved into its chats

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
_LIST_SLUG = re.compile(r"[A-Za-z0-9_-]+")
_LINK = re.compile(r"(?:https?://)?(?:t|telegram)\.me/(.+)", re.IGNORECASE)
_RESOLVE = re.compile(r"tg://resolve\?domain=([A-Za-z0-9_]+)(?:&.*)?", re.IGNORECASE)
_ADDLIST = re.compile(r"tg://addlist\?slug=([^&#]+)(?:[&#].*)?", re.IGNORECASE)


class ChatStatus(StrEnum):
    PENDING = "pending"  # not joined yet
    JOINED = "joined"  # the scraper account is a member
    REQUESTED = "requested"  # waiting for an admin to approve the join request
    FAILED = "failed"  # the chat does not exist or joining failed
    EXPANDED = "expanded"  # a folder link resolved into its chats; nothing more to do for this row


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


@dataclass(frozen=True)
class ChatList:
    slug: str  # case-sensitive, from a t.me/addlist link

    def __str__(self) -> str:
        return f"https://t.me/addlist/{self.slug}"


ChatRef = Username | Invite | ChatId | ChatList


def parse_chat(raw: str | int) -> ChatRef:
    """Accept @username, a t.me or tg://resolve link to a chat, post, topic, invite or folder, or a marked chat ID."""
    if isinstance(raw, int):
        return _chat_id(raw, raw)
    text = raw.strip()
    if re.fullmatch(r"-?\d+", text):
        return _chat_id(int(text), raw)
    resolve = _RESOLVE.fullmatch(text)
    if resolve:
        text = resolve.group(1)
    addlist = _ADDLIST.fullmatch(text)
    if addlist:
        return _chat_list(addlist.group(1), raw)
    link = _LINK.fullmatch(text)
    if link:
        path = re.split(r"[?#]", link.group(1))[0].strip("/")
        # t.me/addlist/<slug> is a shared folder; the slug is case-sensitive.
        if path == "addlist" or path.startswith("addlist/"):
            return _chat_list(path.removeprefix("addlist").removeprefix("/"), raw)
        # t.me/c/<id>/<msg> is a message in a private channel or supergroup.
        private = re.fullmatch(r"c/(\d+)(?:/.*)?", path)
        if private:
            return ChatId(-1_000_000_000_000 - int(private.group(1)))
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


def _chat_list(slug: str, raw: str | int) -> ChatList:
    if _LIST_SLUG.fullmatch(slug):
        return ChatList(slug)
    raise ValueError(f"not a Telegram folder link: {raw}")


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
        case ChatList(slug):
            return "list", slug


def ref_from_row(kind: str, value: str) -> ChatRef:
    match kind:
        case "username":
            return Username(value)
        case "invite":
            return Invite(value)
        case "id":
            return ChatId(int(value))
        case "list":
            return ChatList(value)
    raise ValueError(f"unknown chat kind {kind!r}")


def message_link(chat_id: int, msg_id: int, username: str | None = None) -> str:
    """t.me link to a message. Without a username it works only for members of the chat."""
    if username:
        return f"https://t.me/{username}/{msg_id}"
    if chat_id <= -1_000_000_000_000:  # channels and supergroups: -100<id>
        return f"https://t.me/c/{-chat_id - 1_000_000_000_000}/{msg_id}"
    return f"chat {chat_id} msg {msg_id}"
