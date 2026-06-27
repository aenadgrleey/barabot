"""Telegram message formatting for the reporter. Pure functions, easy to test."""
from __future__ import annotations

# Telegram hard limit is 4096 chars. We aim well below so a notification
# never gets split or rejected.
_TG_MAX = 3800


def _truncate(text: str, limit: int = 600) -> str:
    """Shorten long message bodies to keep notifications scannable."""
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "…"


def format_new(item: dict, score: float | None = None) -> str:
    """Format a 'NEW item detected' notification."""
    chat = item.get("chat_id")
    sender = item.get("sender") or "unknown"
    text = _truncate(item.get("text", ""))
    score_part = f"\nscore: {score:.3f}" if score is not None else ""
    return (
        f"🆕 NEW\n"
        f"chat: {chat}\n"
        f"from: {sender}{score_part}\n\n"
        f"{text}"
    )


def format_item(item: dict) -> str:
    """Format a single item row (used by /get and /latest)."""
    return (
        f"<code>{item['chat_id']}/{item['msg_id']}</code> "
        f"@ {item['created_at']}\n"
        f"from: {item.get('sender') or '—'}\n\n"
        f"{_truncate(item['text'])}"
    )


def format_list(items: list[dict], header: str) -> str:
    """Format a list of items with a short index. Telegram-safe length."""
    if not items:
        return f"{header}\n\n(none yet)"
    lines = [header, ""]
    for i, it in enumerate(items, 1):
        text = _truncate(it["text"], 80).replace("\n", " ")
        sender = (it.get("sender") or "—")[:20]
        lines.append(
            f"{i}. <code>{it['chat_id']}/{it['msg_id']}</code> "
            f"@{it['created_at']} {sender}\n   {text}"
        )
    body = "\n".join(lines)
    if len(body) > _TG_MAX:
        body = body[:_TG_MAX].rstrip() + "\n…(truncated)"
    return body


def format_chat_counts(counts: list[tuple[int, int]]) -> str:
    if not counts:
        return "No items recorded yet."
    lines = ["Items per chat:", ""]
    for chat_id, n in counts:
        lines.append(f"  <code>{chat_id}</code> — {n}")
    return "\n".join(lines)


HELP = (
    "🛒 bara_bot reporter\n\n"
    "Commands:\n"
    "  /start — greeting\n"
    "  /help — this message\n"
    "  /latest [N] — last N items (default 10, max 100)\n"
    "  /chats — counts per chat\n"
    "  /search <text> — substring search\n"
    "  /get <chat_id> <msg_id> — full text of a specific item\n\n"
    "I push a notification every time barabot logs a new item. "
    "Tap /latest to scroll back."
)
