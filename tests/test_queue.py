"""Tests for barabot.core.queue — inbox/outbox/command queue helpers.

RED state: this test module is expected to fail right now because
``barabot.core.queue`` does not yet exist. The GREEN phase will create
that module, add the queue tables to ``barabot.core.db.SCHEMA`` (or a
follow-up migration), and implement the helpers exercised below.

Conventions for the helpers these tests pin down:

* Each queue (``inbox_queue``, ``outbox_queue``, ``command_queue``)
  exposes ``enqueue_*``, ``claim_*``, ``ack_*``, ``nack_*``.
* ``enqueue_*`` returns the row id (int).
* ``enqueue_inbox`` is idempotent on UNIQUE(chat_id, msg_id): a
  duplicate enqueue returns the existing id and does NOT insert a
  second row.
* ``claim_*`` returns at most ``limit`` rows, oldest-first
  (ORDER BY id ASC). It marks the returned rows as ``processing``,
  sets ``claimed_at`` to ``now()`` (or the caller-supplied ``now``),
  and increments ``attempts`` by 1.
* ``ack_*`` deletes the rows identified by id and returns the count
  of rows actually deleted (sqlite ``rowcount`` semantics).
* ``nack_*`` resets the rows to ``pending``, appends ``error`` to
  the existing error text, and clears ``claimed_at``. The lease is
  implicit: a pending row is always claimable.
* The lease (``lease_seconds``) controls reclaim: a row in
  ``processing`` whose ``claimed_at`` is within the lease window is
  SKIPPED by a subsequent claim; a row whose lease has EXPIRED is
  reclaimed (status flipped back to ``processing``, ``attempts``
  incremented, ``claimed_at`` refreshed).
* Two workers claiming concurrently must not see the same row.
* Operations on different queues must not interfere.

All tests use a per-test ``tmp_path`` SQLite file via ``connect()``
from ``barabot.core.db`` so they don't touch the real database.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Callable

import pytest

from barabot.core.db import connect

# Imports below are the ones that should fail in the RED phase.
from barabot.core import queue as q


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def db(tmp_path: Path) -> sqlite3.Connection:
    """Fresh, isolated SQLite DB per test, schema applied via connect()."""
    return connect(tmp_path / "queue.sqlite")


@pytest.fixture()
def inbox_enqueue() -> Callable[..., int]:
    """Bind to the inbox enqueue helper once so each test stays short."""
    return q.enqueue_inbox


@pytest.fixture()
def outbox_enqueue() -> Callable[..., int]:
    return q.enqueue_outbox


@pytest.fixture()
def command_enqueue() -> Callable[..., int]:
    return q.enqueue_command


# ---------------------------------------------------------------------------
# Per-table tests: each test_ function below is parameterized with one
# combination of (label, table, enqueue_fn, make_args, extra_required_fields)
# so the same shape covers all three queues. Where a queue has unique
# semantics (idempotency on UNIQUE for inbox), we write the table-specific
# test instead of using the parametrized one.
# ---------------------------------------------------------------------------


def _inbox_args(**overrides):
    base = dict(
        chat_id=-1001234567890,
        msg_id=1,
        chat_title="Барахолка",
        sender="@alice",
        text="продам велосипед",
        enqueued_at="2026-06-28T12:00:00+00:00",
    )
    base.update(overrides)
    return base


def _outbox_args(**overrides):
    base = dict(
        chat_id=-1001234567890,
        text="ответ",
        reply_to_msg_id=42,
        enqueued_at="2026-06-28T12:00:00+00:00",
    )
    base.update(overrides)
    return base


def _command_args(**overrides):
    base = dict(
        source="telegram",
        command="search",
        args="велосипед",
        enqueued_at="2026-06-28T12:00:00+00:00",
    )
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# inbox_queue
# ---------------------------------------------------------------------------


def test_inbox_enqueue_returns_id_with_all_fields(db, inbox_enqueue):
    row_id = inbox_enqueue(db, **_inbox_args())
    assert isinstance(row_id, int) and row_id > 0
    row = db.execute(
        "SELECT chat_id, msg_id, chat_title, sender, text, enqueued_at, status, attempts "
        "FROM inbox_queue WHERE id = ?",
        (row_id,),
    ).fetchone()
    assert row is not None
    chat_id, msg_id, chat_title, sender, text, enqueued_at, status, attempts = row
    assert chat_id == -1001234567890
    assert msg_id == 1
    assert chat_title == "Барахолка"
    assert sender == "@alice"
    assert text == "продам велосипед"
    assert enqueued_at == "2026-06-28T12:00:00+00:00"
    assert status == "pending"
    assert attempts == 0


def test_inbox_enqueue_idempotent_on_unique_conflict(db, inbox_enqueue):
    """UNIQUE(chat_id, msg_id): re-enqueueing the same (chat, msg) must
    return the SAME id and not insert a second row."""
    first = inbox_enqueue(db, **_inbox_args(msg_id=7))
    second = inbox_enqueue(
        db,
        # different text — should be IGNORED, not upserted.
        **_inbox_args(msg_id=7, text="другой текст", sender="@bob"),
    )
    assert first == second
    count = db.execute(
        "SELECT COUNT(*) FROM inbox_queue WHERE chat_id = ? AND msg_id = ?",
        (-1001234567890, 7),
    ).fetchone()[0]
    assert count == 1


def test_inbox_claim_pending_returns_oldest_first(db, inbox_enqueue):
    for i in range(3):
        inbox_enqueue(db, **_inbox_args(msg_id=i, enqueued_at=f"2026-06-28T12:00:0{i}+00:00"))
    rows = q.claim_inbox(db, limit=10, lease_seconds=60, now="2026-06-28T12:01:00+00:00")
    ids = [r["id"] for r in rows]
    assert ids == sorted(ids), f"expected oldest-first ASC, got {ids}"
    assert len(ids) == 3


def test_inbox_claim_returns_at_most_limit_rows(db, inbox_enqueue):
    for i in range(5):
        inbox_enqueue(db, **_inbox_args(msg_id=i))
    rows = q.claim_inbox(db, limit=2, lease_seconds=60)
    assert len(rows) == 2


def test_inbox_claim_marks_status_processing_and_sets_claimed_at_and_increments_attempts(
    db, inbox_enqueue
):
    inbox_enqueue(db, **_inbox_args(msg_id=11))
    rows = q.claim_inbox(db, limit=1, lease_seconds=60, now="2026-06-28T12:01:00+00:00")
    assert len(rows) == 1
    row = rows[0]
    assert row["status"] == "processing"
    assert row["claimed_at"] == "2026-06-28T12:01:00+00:00"
    assert row["attempts"] == 1
    # DB-level confirmation.
    db_status, db_attempts, db_claimed_at = db.execute(
        "SELECT status, attempts, claimed_at FROM inbox_queue WHERE id = ?",
        (row["id"],),
    ).fetchone()
    assert db_status == "processing"
    assert db_attempts == 1
    assert db_claimed_at == "2026-06-28T12:01:00+00:00"

    # Second claim should increment attempts further on a NEW row.
    inbox_enqueue(db, **_inbox_args(msg_id=12))
    rows2 = q.claim_inbox(
        db, limit=10, lease_seconds=60, now="2026-06-28T12:02:00+00:00"
    )
    by_id = {r["id"]: r for r in rows2}
    assert by_id[row["id"]]["attempts"] == 2
    assert by_id[row["id"]]["claimed_at"] == "2026-06-28T12:02:00+00:00"


def test_inbox_ack_deletes_rows(db, inbox_enqueue):
    rid = inbox_enqueue(db, **_inbox_args(msg_id=100))
    assert q.claim_inbox(db, limit=10, lease_seconds=60)
    deleted = q.ack_inbox(db, [rid])
    assert deleted == 1
    assert db.execute("SELECT COUNT(*) FROM inbox_queue").fetchone()[0] == 0


def test_inbox_ack_returns_count_of_deleted(db, inbox_enqueue):
    ids = [inbox_enqueue(db, **_inbox_args(msg_id=m)) for m in range(3)]
    q.claim_inbox(db, limit=10, lease_seconds=60)
    # Ack only two of the three.
    assert q.ack_inbox(db, ids[:2]) == 2
    # Idempotent: acking again returns 0.
    assert q.ack_inbox(db, ids[:2]) == 0


def test_inbox_nack_resets_to_pending_and_records_error(db, inbox_enqueue):
    rid = inbox_enqueue(db, **_inbox_args(msg_id=200))
    q.claim_inbox(db, limit=10, lease_seconds=60, now="2026-06-28T12:01:00+00:00")
    q.nack_inbox(db, [rid], error="openai 503", now="2026-06-28T12:01:05+00:00")
    status, attempts, error = db.execute(
        "SELECT status, attempts, error FROM inbox_queue WHERE id = ?", (rid,)
    ).fetchone()
    assert status == "pending"
    assert attempts == 1  # nack does NOT reset attempts — it just records the failure
    assert error is not None and "openai 503" in error

    # After nack the row is claimable again (lease cleared ⇒ pending row wins).
    rows = q.claim_inbox(db, limit=10, lease_seconds=60, now="2026-06-28T12:02:00+00:00")
    assert any(r["id"] == rid for r in rows)


# ---------------------------------------------------------------------------
# outbox_queue — same shape, different columns
# ---------------------------------------------------------------------------


def test_outbox_enqueue_returns_id_with_all_fields(db, outbox_enqueue):
    row_id = outbox_enqueue(db, **_outbox_args())
    assert isinstance(row_id, int) and row_id > 0
    row = db.execute(
        "SELECT chat_id, text, reply_to_msg_id, enqueued_at, status, attempts "
        "FROM outbox_queue WHERE id = ?",
        (row_id,),
    ).fetchone()
    assert row is not None
    chat_id, text, reply_to_msg_id, enqueued_at, status, attempts = row
    assert chat_id == -1001234567890
    assert text == "ответ"
    assert reply_to_msg_id == 42
    assert enqueued_at == "2026-06-28T12:00:00+00:00"
    assert status == "pending"
    assert attempts == 0


def test_outbox_claim_pending_returns_oldest_first(db, outbox_enqueue):
    for i in range(3):
        outbox_enqueue(db, **_outbox_args(reply_to_msg_id=i, enqueued_at=f"2026-06-28T12:00:0{i}+00:00"))
    rows = q.claim_outbox(db, limit=10, lease_seconds=60, now="2026-06-28T12:01:00+00:00")
    ids = [r["id"] for r in rows]
    assert ids == sorted(ids)
    assert len(ids) == 3


def test_outbox_claim_returns_at_most_limit_rows(db, outbox_enqueue):
    for i in range(4):
        outbox_enqueue(db, **_outbox_args(reply_to_msg_id=i))
    rows = q.claim_outbox(db, limit=2, lease_seconds=60)
    assert len(rows) == 2


def test_outbox_claim_marks_status_processing_and_sets_claimed_at_and_increments_attempts(
    db, outbox_enqueue
):
    outbox_enqueue(db, **_outbox_args(reply_to_msg_id=1))
    rows = q.claim_outbox(db, limit=1, lease_seconds=60, now="2026-06-28T12:01:00+00:00")
    assert rows[0]["status"] == "processing"
    assert rows[0]["claimed_at"] == "2026-06-28T12:01:00+00:00"
    assert rows[0]["attempts"] == 1


def test_outbox_ack_deletes_rows(db, outbox_enqueue):
    rid = outbox_enqueue(db, **_outbox_args(reply_to_msg_id=99))
    q.claim_outbox(db, limit=10, lease_seconds=60)
    assert q.ack_outbox(db, [rid]) == 1
    assert db.execute("SELECT COUNT(*) FROM outbox_queue").fetchone()[0] == 0


def test_outbox_ack_returns_count_of_deleted(db, outbox_enqueue):
    ids = [outbox_enqueue(db, **_outbox_args(reply_to_msg_id=m)) for m in range(3)]
    q.claim_outbox(db, limit=10, lease_seconds=60)
    assert q.ack_outbox(db, ids[:2]) == 2
    assert q.ack_outbox(db, ids[:2]) == 0


def test_outbox_nack_resets_to_pending_and_records_error(db, outbox_enqueue):
    rid = outbox_enqueue(db, **_outbox_args(reply_to_msg_id=300))
    q.claim_outbox(db, limit=10, lease_seconds=60)
    q.nack_outbox(db, [rid], error="telegram flood_wait=30")
    status, error = db.execute(
        "SELECT status, error FROM outbox_queue WHERE id = ?", (rid,)
    ).fetchone()
    assert status == "pending"
    assert error is not None and "flood_wait" in error


# ---------------------------------------------------------------------------
# command_queue
# ---------------------------------------------------------------------------


def test_command_enqueue_returns_id_with_all_fields(db, command_enqueue):
    row_id = command_enqueue(db, **_command_args())
    assert isinstance(row_id, int) and row_id > 0
    row = db.execute(
        "SELECT source, command, args, enqueued_at, status, attempts "
        "FROM command_queue WHERE id = ?",
        (row_id,),
    ).fetchone()
    assert row is not None
    source, command, args, enqueued_at, status, attempts = row
    assert source == "telegram"
    assert command == "search"
    assert args == "велосипед"
    assert enqueued_at == "2026-06-28T12:00:00+00:00"
    assert status == "pending"
    assert attempts == 0


def test_command_claim_pending_returns_oldest_first(db, command_enqueue):
    for i in range(3):
        command_enqueue(db, **_command_args(args=f"q{i}", enqueued_at=f"2026-06-28T12:00:0{i}+00:00"))
    rows = q.claim_command(db, limit=10, lease_seconds=60, now="2026-06-28T12:01:00+00:00")
    ids = [r["id"] for r in rows]
    assert ids == sorted(ids)
    assert len(ids) == 3


def test_command_claim_returns_at_most_limit_rows(db, command_enqueue):
    for i in range(5):
        command_enqueue(db, **_command_args(args=f"q{i}"))
    rows = q.claim_command(db, limit=2, lease_seconds=60)
    assert len(rows) == 2


def test_command_claim_marks_status_processing_and_sets_claimed_at_and_increments_attempts(
    db, command_enqueue
):
    command_enqueue(db, **_command_args(args="one"))
    rows = q.claim_command(db, limit=1, lease_seconds=60, now="2026-06-28T12:01:00+00:00")
    assert rows[0]["status"] == "processing"
    assert rows[0]["claimed_at"] == "2026-06-28T12:01:00+00:00"
    assert rows[0]["attempts"] == 1


def test_command_ack_deletes_rows(db, command_enqueue):
    rid = command_enqueue(db, **_command_args(args="x"))
    q.claim_command(db, limit=10, lease_seconds=60)
    assert q.ack_command(db, [rid]) == 1
    assert db.execute("SELECT COUNT(*) FROM command_queue").fetchone()[0] == 0


def test_command_ack_returns_count_of_deleted(db, command_enqueue):
    ids = [command_enqueue(db, **_command_args(args=f"q{i}")) for i in range(3)]
    q.claim_command(db, limit=10, lease_seconds=60)
    assert q.ack_command(db, ids[:2]) == 2
    assert q.ack_command(db, ids[:2]) == 0


def test_command_nack_resets_to_pending_and_records_error(db, command_enqueue):
    rid = command_enqueue(db, **_command_args(args="oops"))
    q.claim_command(db, limit=10, lease_seconds=60)
    q.nack_command(db, [rid], error="unknown command")
    status, error = db.execute(
        "SELECT status, error FROM command_queue WHERE id = ?", (rid,)
    ).fetchone()
    assert status == "pending"
    assert error is not None and "unknown command" in error


# ---------------------------------------------------------------------------
# Cross-cutting tests
# ---------------------------------------------------------------------------


def test_claim_skips_processing_rows_within_lease(db, inbox_enqueue):
    """While a row is processing AND its lease has not expired, a new
    claim with a smaller limit must not return it again."""
    inbox_enqueue(db, **_inbox_args(msg_id=1))
    first = q.claim_inbox(db, limit=10, lease_seconds=60, now="2026-06-28T12:00:00+00:00")
    assert len(first) == 1
    # Second claim 10s later, same lease window — must skip the in-flight row.
    second = q.claim_inbox(db, limit=10, lease_seconds=60, now="2026-06-28T12:00:10+00:00")
    assert second == []


def test_claim_reclaims_processing_rows_whose_lease_expired(db, inbox_enqueue):
    inbox_enqueue(db, **_inbox_args(msg_id=1))
    first = q.claim_inbox(db, limit=10, lease_seconds=30, now="2026-06-28T12:00:00+00:00")
    rid = first[0]["id"]
    # 5 minutes later — lease is well past 30s.
    second = q.claim_inbox(db, limit=10, lease_seconds=30, now="2026-06-28T12:05:00+00:00")
    assert len(second) == 1 and second[0]["id"] == rid
    # attempts must have been incremented past the initial claim.
    assert second[0]["attempts"] >= 2


def test_claim_does_not_double_claim_rows_other_worker_just_took(
    db, inbox_enqueue
):
    """Simulated race: worker A claims row 1 with limit=1, then worker
    B claims with limit=1 while A's lease is still valid. Row 1 is
    A's; there are no other rows. B must see nothing."""
    inbox_enqueue(db, **_inbox_args(msg_id=1))
    a = q.claim_inbox(db, limit=1, lease_seconds=60, now="2026-06-28T12:00:00+00:00")
    assert len(a) == 1
    # 10 seconds later — well within A's 60s lease. B must not steal row 1.
    b = q.claim_inbox(db, limit=1, lease_seconds=60, now="2026-06-28T12:00:10+00:00")
    assert b == []
    # Once A acks, the row is gone for good (delete, not release).
    q.ack_inbox(db, [a[0]["id"]])
    c = q.claim_inbox(db, limit=1, lease_seconds=60, now="2026-06-28T12:00:10+00:00")
    assert c == []


def test_two_queues_are_independent(db, inbox_enqueue, outbox_enqueue):
    """Claiming from inbox must not touch outbox and vice versa."""
    in_id = inbox_enqueue(db, **_inbox_args(msg_id=1))
    out_id = outbox_enqueue(db, **_outbox_args(reply_to_msg_id=1))
    inbox_rows = q.claim_inbox(db, limit=10, lease_seconds=60)
    outbox_rows = q.claim_outbox(db, limit=10, lease_seconds=60)
    assert len(inbox_rows) == 1 and inbox_rows[0]["id"] == in_id
    assert len(outbox_rows) == 1 and outbox_rows[0]["id"] == out_id
    # Status of each row's *own* queue is processing; the other queue's
    # row is still pending because it wasn't claimed.
    assert inbox_rows[0]["status"] == "processing"
    assert outbox_rows[0]["status"] == "processing"
    out_status = db.execute(
        "SELECT status FROM outbox_queue WHERE id = ?", (out_id,)
    ).fetchone()[0]
    in_status = db.execute(
        "SELECT status FROM inbox_queue WHERE id = ?", (in_id,)
    ).fetchone()[0]
    assert out_status == "processing"
    assert in_status == "processing"
