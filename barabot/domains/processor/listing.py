"""`ListingProcessor`: assemble a listing, save its version, match subscriptions.

Steps of one run, each a DBOS step:

1. assemble   join live messages into a Snapshot, hash; stop if nothing changed
2. save_version   compare-and-set the snapshot and bump `version`
3. load_subscriptions   the active subscriptions of the listing's chat
4. decide_text   one Jev request over the text for every subscription
5. decide_photos   only if some subscription answered "unclear" and the
   listing has photos: the same questions for those subscriptions, with photos
6. save_decisions   write `matches` for this version, mark it decided

The snapshot from step 1 is the only place content is read; later steps
get it from the workflow.

Notification routing (new / updated / unmatched / removed) reads `matches`
and arrives with the bot slice.

A run is identified by `listing:{key}:{rev}`. Every scraper event cancels
the previous revision and schedules the next one with a delay, which
debounces bursts such as album uploads. Cancellation misses races and
crashes, so the steps that read state also check `listings.rev` against
their own `rev` and exit quietly when a newer revision exists.

A run can stop after saving a version and before deciding it. The next run
then sees an unchanged hash, so "unchanged" also requires
`decided_version == version`; otherwise it decides the current version.

The processor is a DBOS configured instance. DBOS stores the class name
and `config_name` with every run and looks the instance up by them when
it recovers runs after a restart. Two rules follow:

- Construct the instance before `DBOS.launch()`, or runs recovered at
  launch find no instance. The queue can only be registered after
  launch, hence the separate `open()`.
- One instance per `config_name` per process. Renaming the class name
  below or the config name orphans pending runs.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from dataclasses import dataclass
from pathlib import Path

from dbos import DBOS, DBOSConfiguredInstance, Queue, SetEnqueueOptions, SetWorkflowID

from barabot.core.db import ListingKey, connect, transaction
from barabot.core.logs import notice
from barabot.domains.processor.budget import BudgetExhausted
from barabot.domains.processor.matching import (
    REJECTED_IMAGES,
    Decider,
    Decision,
    Subscription,
    is_transient,
)

log = logging.getLogger(__name__)

LISTINGS_QUEUE = "listings"

# Jev calls: cancellation interrupts the HTTP request (preemptible), and
# transient API errors are retried durably, on top of the SDK's own retries.
_JEV_STEP = dict(
    preemptible=True,
    timeout_seconds=120.0,
    retries_allowed=True,
    max_attempts=4,
    interval_seconds=5.0,
    backoff_rate=3.0,
    should_retry=is_transient,
)


@dataclass(frozen=True)
class Snapshot:
    """A listing's content as assembled from its live messages."""

    text: str
    media: list[dict]
    removed: bool
    content_hash: str

    def photo_files(self) -> list[str]:
        """Downloaded photo files, by name under the media directory."""
        return [m["file"] for m in self.media if m.get("type") == "photo" and m.get("file")]


@dataclass(frozen=True)
class Assembled:
    """Result of step 1.

    outcome:  stale       a newer revision exists
              unchanged   content and its decisions are both saved
              changed     content differs from the saved version
              undecided   content is saved as `seen_version`, its decisions are not
    seen_version: the version read; the compare-and-set base for `changed`,
              the version to decide for `undecided`
    """

    outcome: str
    snapshot: Snapshot | None = None
    seen_version: int = 0


@DBOS.dbos_class("listing_processor")
class ListingProcessor(DBOSConfiguredInstance):
    def __init__(
        self,
        db_path: Path,
        media_dir: Path,
        decider: Decider,
        *,
        debounce_seconds: float = 3.0,
        name: str = "default",
    ) -> None:
        super().__init__(config_name=name)
        self._db_path = db_path
        self._media_dir = media_dir
        self._decider = decider
        self._debounce_seconds = debounce_seconds
        self._queue: Queue | None = None

    async def open(self) -> None:
        """Register the queue. Call after DBOS.launch()."""
        self._queue = await DBOS.register_queue_async(
            LISTINGS_QUEUE, worker_concurrency=8, polling_interval_sec=0.2
        )

    async def schedule(self, key: ListingKey, rev: int) -> None:
        """Replace the pending or running run of `key` with revision `rev`."""
        if self._queue is None:
            raise RuntimeError("ListingProcessor.open() was not called")
        if rev > 1:
            # cancel_children also drops bot.notify runs of the old revision
            # once routing enqueues them.
            await DBOS.cancel_workflow_async(key.workflow_id(rev - 1), cancel_children=True)
        with SetWorkflowID(key.workflow_id(rev)), SetEnqueueOptions(delay_seconds=self._debounce_seconds):
            await self._queue.enqueue_async(self.process, key.chat_id, key.album, key.ref, rev)

    @DBOS.workflow()
    async def process(self, chat_id: int, album: bool, ref: int, rev: int) -> str:
        key = ListingKey(chat_id, album, ref)
        assembled = await self.assemble(key, rev)
        if assembled.outcome in ("stale", "unchanged"):
            return assembled.outcome
        snapshot = assembled.snapshot
        if assembled.outcome == "changed":
            version = await self.save_version(key, rev, snapshot, assembled.seen_version)
            if version is None:
                return "stale"
        else:  # undecided
            version = assembled.seen_version

        decisions: list[Decision] = []
        if not snapshot.removed:
            subscriptions = await self.load_subscriptions(key, rev, version)
            if subscriptions is None:
                return "stale"
            if subscriptions:
                first = await self.decide_text(snapshot.text, subscriptions)
                if first is None:
                    return "budget exhausted"  # stays undecided; redecided once the limit is raised
                decisions = first
                pending = {d.subscription_id for d in decisions if d.needs_photos}
                photos = snapshot.photo_files()
                if pending and photos:
                    retry = [s for s in subscriptions if s.id in pending]
                    second = await self.decide_photos(snapshot.text, retry, photos)
                    if second is not None:
                        by_id = {d.subscription_id: d for d in second}
                        decisions = [by_id.get(d.subscription_id, d) for d in decisions]
        matched = await self.save_decisions(key, version, decisions)
        return f"version {version}: {matched} match(es)"

    @DBOS.step()
    async def assemble(self, key: ListingKey, rev: int) -> Assembled:
        return await asyncio.to_thread(self._assemble_blocking, key, rev)

    @DBOS.step()
    async def save_version(self, key: ListingKey, rev: int, snapshot: Snapshot, seen_version: int) -> int | None:
        return await asyncio.to_thread(self._save_version_blocking, key, rev, snapshot, seen_version)

    @DBOS.step()
    async def load_subscriptions(self, key: ListingKey, rev: int, version: int) -> list[Subscription] | None:
        """None when a newer revision or version exists: do not pay for a stale decision."""
        return await asyncio.to_thread(self._load_subscriptions_blocking, key, rev, version)

    @DBOS.step(**_JEV_STEP)
    async def decide_text(self, text: str, subscriptions: list[Subscription]) -> list[Decision] | None:
        """None when the spend limit is reached."""
        try:
            return await self._decider.decide(text, subscriptions)
        except BudgetExhausted:
            return None

    @DBOS.step(**_JEV_STEP)
    async def decide_photos(
        self, text: str, subscriptions: list[Subscription], photo_files: list[str]
    ) -> list[Decision] | None:
        """None when no photo file is on disk or Jev rejects them; the text answers then stand."""
        paths = [self._media_dir / name for name in photo_files]
        paths = [p for p in paths if p.is_file()]
        if not paths:
            return None
        try:
            return await self._decider.decide(text, subscriptions, paths)
        except BudgetExhausted:
            return None
        except REJECTED_IMAGES as exc:
            log.warning("Jev rejected photos %s: %s", [p.name for p in paths], exc)
            return None

    @DBOS.step()
    async def save_decisions(self, key: ListingKey, version: int, decisions: list[Decision]) -> int:
        return await asyncio.to_thread(self._save_decisions_blocking, key, version, decisions)

    # Blocking halves of the steps; they run in a worker thread.

    def _assemble_blocking(self, key: ListingKey, rev: int) -> Assembled:
        conn = connect(self._db_path)
        try:
            listing = conn.execute(
                "SELECT rev, content_hash, version, decided_version FROM listings "
                "WHERE chat_id = ? AND album = ? AND ref = ?",
                key,
            ).fetchone()
            if listing is None or listing["rev"] != rev:
                return Assembled("stale")
            rows = conn.execute(
                "SELECT text, media FROM messages "
                "WHERE chat_id = ? AND listing_album = ? AND listing_ref = ? AND deleted = 0 "
                "ORDER BY msg_id",
                key,
            ).fetchall()
        finally:
            conn.close()

        text = "\n\n".join(row["text"] for row in rows if row["text"])
        media = [item for row in rows for item in json.loads(row["media"])]
        removed = not rows
        digest = hashlib.sha256(
            json.dumps({"text": text, "media": media, "removed": removed}, sort_keys=True).encode()
        ).hexdigest()
        snapshot = Snapshot(text, media, removed, digest)
        version = listing["version"]
        if digest != listing["content_hash"]:
            return Assembled("changed", snapshot, version)
        if listing["decided_version"] != version:
            return Assembled("undecided", snapshot, version)
        return Assembled("unchanged")

    def _save_version_blocking(
        self, key: ListingKey, rev: int, snapshot: Snapshot, seen_version: int
    ) -> int | None:
        """Compare-and-set on version; also refuse if a newer revision arrived."""
        conn = connect(self._db_path)
        try:
            with transaction(conn):
                row = conn.execute(
                    "UPDATE listings SET text = ?, media = ?, content_hash = ?, removed = ?, "
                    "version = version + 1 "
                    "WHERE chat_id = ? AND album = ? AND ref = ? AND version = ? AND rev = ? "
                    "RETURNING version",
                    (
                        snapshot.text,
                        json.dumps(snapshot.media),
                        snapshot.content_hash,
                        int(snapshot.removed),
                        *key,
                        seen_version,
                        rev,
                    ),
                ).fetchone()
        finally:
            conn.close()
        return None if row is None else int(row["version"])

    def _load_subscriptions_blocking(self, key: ListingKey, rev: int, version: int) -> list[Subscription] | None:
        conn = connect(self._db_path)
        try:
            listing = conn.execute(
                "SELECT rev, version FROM listings WHERE chat_id = ? AND album = ? AND ref = ?",
                key,
            ).fetchone()
            if listing is None or listing["rev"] != rev or listing["version"] != version:
                return None
            return [
                Subscription(row["id"], row["author"], row["author_not"], row["query"])
                for row in conn.execute(
                    "SELECT s.id, s.author, s.author_not, s.query FROM subscriptions s "
                    "JOIN subscription_chats c ON c.subscription_id = s.id "
                    "WHERE c.chat_id = ? AND s.active = 1 ORDER BY s.id",
                    (key.chat_id,),
                )
            ]
        finally:
            conn.close()

    def _save_decisions_blocking(self, key: ListingKey, version: int, decisions: list[Decision]) -> int:
        conn = connect(self._db_path)
        try:
            with transaction(conn):
                conn.executemany(
                    "INSERT INTO matches (chat_id, album, ref, version, subscription_id, author_p, choice, "
                    "confidence, round, matched, p_fits) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT (chat_id, album, ref, version, subscription_id) DO UPDATE SET "
                    "author_p = excluded.author_p, choice = excluded.choice, confidence = excluded.confidence, "
                    "round = excluded.round, matched = excluded.matched, p_fits = excluded.p_fits, "
                    "decided_at = datetime('now')",
                    [
                        (*key, version, d.subscription_id, d.author_p, d.choice, d.confidence, d.round,
                         int(d.matched), d.p_fits)
                        for d in decisions
                    ],
                )
                conn.execute(
                    "UPDATE listings SET decided_version = ? WHERE chat_id = ? AND album = ? AND ref = ? AND version = ?",
                    (version, *key, version),
                )
            matched = [d for d in decisions if d.matched]
            if decisions:
                self._log_decisions(conn, key, version, decisions, matched)
        finally:
            conn.close()
        return len(matched)

    def _log_decisions(
        self, conn, key: ListingKey, version: int, decisions: list[Decision], matched: list[Decision]
    ) -> None:
        """One info line per decided listing; one notice per match, which aenaut-notify sends to Telegram."""
        listing = conn.execute(
            "SELECT l.text, min(m.msg_id) AS msg_id FROM listings l JOIN messages m "
            "ON m.chat_id = l.chat_id AND m.listing_album = l.album AND m.listing_ref = l.ref "
            "WHERE l.chat_id = ? AND l.album = ? AND l.ref = ?",
            key,
        ).fetchone()
        link = message_link(key.chat_id, listing["msg_id"]) if listing and listing["msg_id"] else "?"
        rounds = {d.round for d in decisions}
        log.info(
            "Decided %s v%d: %d subscription(s), photo round %s, %d match(es)",
            link, version, len(decisions), "yes" if 2 in rounds else "no", len(matched),
        )
        if not matched:
            return
        queries = dict(
            conn.execute(
                f"SELECT id, query FROM subscriptions WHERE id IN ({','.join('?' * len(matched))})",
                [d.subscription_id for d in matched],
            ).fetchall()
        )
        snippet = " ".join((listing["text"] or "").split())[:160] if listing else ""
        for d in matched:
            notice(
                log,
                "Match: subscription #%d «%s» (author %.2f, fits %.2f, round %d) ← %s %s",
                d.subscription_id, queries.get(d.subscription_id, "?"), d.author_p, d.p_fits, d.round,
                link, snippet or "[без текста]",
            )


def message_link(chat_id: int, msg_id: int) -> str:
    """t.me link to a message; works for members of the chat."""
    if chat_id <= -1_000_000_000_000:  # channels and supergroups: -100<id>
        return f"https://t.me/c/{-chat_id - 1_000_000_000_000}/{msg_id}"
    return f"chat {chat_id} msg {msg_id}"
