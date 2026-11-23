"""`ListingProcessor`: assemble a listing and save its new version.

This slice covers steps 1 and 2 of docs/architecture.html. Embedding,
routing and notification enqueueing arrive with later slices.

A run is identified by `listing:{key}:{rev}`. Every scraper event
cancels the previous revision and schedules the next one with a delay,
which debounces bursts such as album uploads. Cancellation misses races
and crashes, so each step also checks `listings.rev` against its own
`rev` and exits quietly when a newer revision exists.

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
from pathlib import Path

from dbos import DBOS, DBOSConfiguredInstance, Queue, SetEnqueueOptions, SetWorkflowID

from barabot.core.db import ListingKey, connect, transaction

LISTINGS_QUEUE = "listings"


@DBOS.dbos_class("listing_processor")
class ListingProcessor(DBOSConfiguredInstance):
    def __init__(self, db_path: Path, *, debounce_seconds: float = 3.0, name: str = "default") -> None:
        super().__init__(config_name=name)
        self._db_path = db_path
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
        snapshot = await self.assemble(key, rev)
        if snapshot["outcome"] != "changed":
            return snapshot["outcome"]
        version = await self.save_version(key, rev, snapshot)
        return "stale" if version is None else f"version {version}"

    @DBOS.step()
    async def assemble(self, key: ListingKey, rev: int) -> dict:
        return await asyncio.to_thread(self._assemble_blocking, key, rev)

    @DBOS.step()
    async def save_version(self, key: ListingKey, rev: int, snapshot: dict) -> int | None:
        return await asyncio.to_thread(self._save_version_blocking, key, rev, snapshot)

    # Blocking halves of the steps; they run in a worker thread.

    def _assemble_blocking(self, key: ListingKey, rev: int) -> dict:
        conn = connect(self._db_path)
        try:
            listing = conn.execute(
                "SELECT rev, content_hash, version FROM listings WHERE chat_id = ? AND album = ? AND ref = ?",
                key,
            ).fetchone()
            if listing is None or listing["rev"] != rev:
                return {"outcome": "stale"}
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
        if digest == listing["content_hash"]:
            return {"outcome": "unchanged"}
        return {
            "outcome": "changed",
            "text": text,
            "media": media,
            "removed": removed,
            "content_hash": digest,
            "seen_version": listing["version"],
        }


    def _save_version_blocking(self, key: ListingKey, rev: int, snapshot: dict) -> int | None:
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
                        snapshot["text"],
                        json.dumps(snapshot["media"]),
                        snapshot["content_hash"],
                        int(snapshot["removed"]),
                        *key,
                        snapshot["seen_version"],
                        rev,
                    ),
                ).fetchone()
        finally:
            conn.close()
        return None if row is None else int(row["version"])
