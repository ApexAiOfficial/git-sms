"""Durable state in one SQLite file: intake queue, write journal, outbound ledger,
limits, confirmations, context, opt-outs and audit.

Senders are identified by keyed hashes (HMAC-SHA256 with GIT_SMS_SECRET_KEY). What must
be kept briefly in recoverable form (the queued message with its number, reply text,
pending changes, approved-but-unattempted writes, MORE pages) is sealed with AES-GCM
under a key derived from the same secret, and is deleted as soon as it is no longer
needed. Nothing in the file reveals a phone number or message content without the
secret, which never lives in the database.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import sqlite3
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from git_sms.config import ConfigError

SCHEMA_VERSION = 2
SCHEMA = """
CREATE TABLE inbound (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    message_sid TEXT NOT NULL UNIQUE,
    sender_key TEXT NOT NULL,
    received_at REAL NOT NULL,
    status TEXT NOT NULL,             -- queued, processing, done, failed
    attempts INTEGER NOT NULL DEFAULT 0,
    payload BLOB                      -- sealed; cleared once done or failed
);
CREATE INDEX inbound_status ON inbound (status, sender_key);
CREATE TABLE outbound (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_sid TEXT NOT NULL,
    sender_key TEXT NOT NULL,
    created_at REAL NOT NULL,
    status TEXT NOT NULL,             -- our send lifecycle: pending, sending, accepted, failed, unknown
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at REAL NOT NULL,
    segments INTEGER NOT NULL,
    provider_sid TEXT UNIQUE,
    error TEXT,
    delivery TEXT,                    -- Twilio's delivery status from callbacks; only ever advances
    payload BLOB                      -- sealed; cleared once the provider accepted or refused it
);
CREATE INDEX outbound_status ON outbound (status, next_attempt_at);
CREATE INDEX outbound_sender ON outbound (sender_key, status);
CREATE TABLE delivery_events (        -- callbacks that arrived before the send recorded its SID
    provider_sid TEXT PRIMARY KEY,
    delivery TEXT NOT NULL,
    error TEXT,
    at REAL NOT NULL
);
CREATE TABLE operations (
    op_id TEXT PRIMARY KEY,           -- derived from the MessageSid that approved the write
    sender_key TEXT NOT NULL,
    action TEXT NOT NULL,
    repo TEXT NOT NULL,
    number INTEGER,
    fingerprint TEXT NOT NULL,
    status TEXT NOT NULL,             -- confirmed (approved, not attempted), executing, done, failed, unknown,
                                      -- observed (target state seen on GitHub, not proven to be this write)
    url TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    command BLOB                      -- sealed intent; cleared once an attempt starts
);
CREATE INDEX operations_fingerprint ON operations (sender_key, fingerprint);
CREATE TABLE counters (
    key TEXT PRIMARY KEY,
    window_start INTEGER NOT NULL,
    count INTEGER NOT NULL
);
CREATE TABLE pending (
    sender_key TEXT PRIMARY KEY,
    code_mac TEXT NOT NULL,
    command BLOB NOT NULL,
    expires_at REAL NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    action TEXT NOT NULL,
    repo TEXT NOT NULL,
    number INTEGER,
    fingerprint TEXT NOT NULL
);
CREATE TABLE context (
    sender_key TEXT PRIMARY KEY,
    default_repo TEXT,
    pages BLOB,
    pages_expire_at REAL
);
CREATE TABLE opt_outs (
    sender_key TEXT PRIMARY KEY,
    at REAL NOT NULL
);
CREATE TABLE audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at REAL NOT NULL,
    message_sid TEXT,
    sender_key TEXT NOT NULL,
    alias TEXT,
    action TEXT NOT NULL,
    repo TEXT,
    outcome TEXT NOT NULL,
    detail TEXT,
    url TEXT
);
"""
SETTLED_OUTBOUND = ("accepted", "failed", "unknown")
# Twilio delivery statuses by progress: a callback may only move a message forward.
DELIVERY_RANK = {
    "accepted": 1,
    "scheduled": 1,
    "queued": 1,
    "sending": 2,
    "sent": 3,
    "partially_delivered": 4,
    "delivered": 5,
    "undelivered": 5,
    "failed": 5,
    "canceled": 5,
    "read": 6,
}
OPERATION_COLUMNS = "op_id, action, repo, number, status, url, created_at"


@dataclass(frozen=True)
class Job:
    message_sid: str
    sender_key: str
    payload: dict
    attempts: int


@dataclass(frozen=True)
class OutboundMessage:
    id: int
    message_sid: str
    sender_key: str
    payload: dict
    segments: int
    attempts: int


@dataclass(frozen=True)
class Operation:
    op_id: str
    action: str
    repo: str
    number: int | None
    status: str
    url: str | None
    created_at: float


@dataclass(frozen=True)
class AuditEntry:
    at: float
    message_sid: str | None
    sender_key: str
    alias: str | None
    action: str
    repo: str | None
    outcome: str
    detail: str | None
    url: str | None


class Sealer:
    """AES-256-GCM with a key derived from the service secret; the sender key is bound as associated data."""

    def __init__(self, secret: bytes):
        self._aead = AESGCM(hashlib.sha256(b"git-sms seal v1\0" + secret).digest())

    def seal(self, value: object, context: str) -> bytes:
        nonce = os.urandom(12)
        return nonce + self._aead.encrypt(nonce, json.dumps(value).encode(), context.encode())

    def open(self, blob: bytes, context: str) -> object:
        return json.loads(self._aead.decrypt(blob[:12], blob[12:], context.encode()))


class Store:
    def __init__(self, path: str, secret_key: str, clock: Callable[[], float] = time.time):
        self._clock = clock
        self._secret = secret_key.encode()
        self._sealer = Sealer(self._secret)
        self._lock = threading.Lock()
        if path != ":memory:" and not os.path.exists(path):
            os.close(os.open(path, os.O_CREAT | os.O_WRONLY, 0o600))
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        if path != ":memory:":
            self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA busy_timeout=5000")
        self._migrate()

    def _migrate(self) -> None:
        version = self._db.execute("PRAGMA user_version").fetchone()[0]
        tables = self._db.execute("SELECT count(*) FROM sqlite_master WHERE type = 'table'").fetchone()[0]
        if version == 0 and tables == 0:
            self._db.executescript(f"BEGIN; {SCHEMA} PRAGMA user_version = {SCHEMA_VERSION}; COMMIT;")
        elif version != SCHEMA_VERSION:
            raise ConfigError(
                f"database schema version {version} is not {SCHEMA_VERSION}; it was made by another "
                "Git SMS release (0.2 pre-release databases cannot be upgraded: start a new one)"
            )

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def now(self) -> float:
        return self._clock()

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                yield self._db
                self._db.execute("COMMIT")
            except BaseException:
                self._db.execute("ROLLBACK")
                raise

    def _mac(self, *parts: str) -> str:
        return hmac.new(self._secret, "\0".join(parts).encode(), hashlib.sha256).hexdigest()

    def sender_key(self, phone: str) -> str:
        return hmac.new(self._secret, phone.encode(), hashlib.sha256).hexdigest()[:32]

    def fingerprint(self, text: str) -> str:
        """Keyed digest of a write's content, to recognise a repeat without storing the content."""
        return self._mac("fingerprint", text)[:32]

    def op_id(self, message_sid: str) -> str:
        """Public, unguessable id for the write a message approved (embedded in GitHub bodies)."""
        return self._mac("op", message_sid)[:20]

    # Intake queue ------------------------------------------------------------------

    def enqueue_inbound(self, message_sid: str, sender_key: str, payload: dict, max_waiting: int | None = None) -> str:
        """Durably queue a message before it is acknowledged.

        Returns "queued", "duplicate" (seen before, even when the queue is full) or "full". The
        capacity check and the insert are one transaction, so concurrent webhooks, even from
        several processes sharing the file, can never push the queue past max_waiting.
        """
        with self._transaction() as db:
            if db.execute("SELECT 1 FROM inbound WHERE message_sid = ?", (message_sid,)).fetchone():
                return "duplicate"
            if max_waiting is not None:
                waiting = db.execute(
                    "SELECT count(*) FROM inbound WHERE status IN ('queued', 'processing')"
                ).fetchone()[0]
                if waiting >= max_waiting:
                    return "full"
            db.execute(
                "INSERT INTO inbound (message_sid, sender_key, received_at, status, payload) "
                "VALUES (?, ?, ?, 'queued', ?)",
                (message_sid, sender_key, self._clock(), self._sealer.seal(payload, sender_key)),
            )
        return "queued"

    def claim_inbound(self) -> Job | None:
        """Take the oldest queued message whose sender has nothing in progress (per-sender FIFO)."""
        with self._transaction() as db:
            row = db.execute(
                "SELECT message_sid, sender_key, payload, attempts FROM inbound WHERE status = 'queued' "
                "AND sender_key NOT IN (SELECT sender_key FROM inbound WHERE status = 'processing') "
                "ORDER BY seq LIMIT 1"
            ).fetchone()
            if not row:
                return None
            db.execute(
                "UPDATE inbound SET status = 'processing', attempts = attempts + 1 WHERE message_sid = ?", (row[0],)
            )
        return Job(row[0], row[1], self._sealer.open(row[2], row[1]), row[3] + 1)

    def complete_inbound(self, job: Job, replies: list[tuple[dict, int]]) -> None:
        """Atomically record the replies to send and retire the message's sealed payload."""
        now = self._clock()
        with self._transaction() as db:
            for payload, segments in replies:
                db.execute(
                    "INSERT INTO outbound (message_sid, sender_key, created_at, status, next_attempt_at, segments, "
                    "payload) VALUES (?, ?, ?, 'pending', ?, ?, ?)",
                    (job.message_sid, job.sender_key, now, now, segments, self._sealer.seal(payload, job.sender_key)),
                )
            db.execute("UPDATE inbound SET status = 'done', payload = NULL WHERE message_sid = ?", (job.message_sid,))

    def fail_inbound(self, job: Job) -> None:
        with self._transaction() as db:
            db.execute("UPDATE inbound SET status = 'failed', payload = NULL WHERE message_sid = ?", (job.message_sid,))

    def requeue_inbound(self, job: Job) -> None:
        with self._transaction() as db:
            db.execute("UPDATE inbound SET status = 'queued' WHERE message_sid = ?", (job.message_sid,))

    def inbound_status(self, message_sid: str) -> str | None:
        with self._lock:
            row = self._db.execute("SELECT status FROM inbound WHERE message_sid = ?", (message_sid,)).fetchone()
        return row[0] if row else None

    def recover(self) -> dict[str, int]:
        """After a restart: re-queue interrupted messages; never resend a reply whose send was in flight."""
        with self._transaction() as db:
            requeued = db.execute("UPDATE inbound SET status = 'queued' WHERE status = 'processing'").rowcount
            unknown = db.execute(
                "UPDATE outbound SET status = 'unknown', error = 'interrupted while sending', payload = NULL "
                "WHERE status = 'sending'"
            ).rowcount
        return {"requeued": requeued, "unknown_sends": unknown}

    # Outbound ledger ---------------------------------------------------------------

    def claim_outbound(self) -> OutboundMessage | None:
        """The oldest ready reply whose sender has no earlier reply waiting or in flight.

        One sender's replies are handed to Twilio one at a time, in the order they were
        written (carriers still do not guarantee delivery order).
        """
        with self._transaction() as db:
            row = db.execute(
                "SELECT id, message_sid, sender_key, payload, segments, attempts FROM outbound o "
                "WHERE status = 'pending' AND next_attempt_at <= ? AND NOT EXISTS ("
                "  SELECT 1 FROM outbound p WHERE p.sender_key = o.sender_key AND p.id < o.id "
                "  AND p.status IN ('pending', 'sending')"
                ") ORDER BY id LIMIT 1",
                (self._clock(),),
            ).fetchone()
            if not row:
                return None
            db.execute("UPDATE outbound SET status = 'sending', attempts = attempts + 1 WHERE id = ?", (row[0],))
        return OutboundMessage(row[0], row[1], row[2], self._sealer.open(row[3], row[2]), row[4], row[5] + 1)

    def settle_outbound(
        self, message_id: int, status: str, *, provider_sid: str | None = None, error: str = ""
    ) -> None:
        with self._transaction() as db:
            early = None
            if provider_sid:
                early = db.execute(
                    "SELECT delivery, error FROM delivery_events WHERE provider_sid = ?", (provider_sid,)
                ).fetchone()
                db.execute("DELETE FROM delivery_events WHERE provider_sid = ?", (provider_sid,))
            db.execute(
                "UPDATE outbound SET status = ?, provider_sid = ?, error = ?, delivery = ?, payload = NULL "
                "WHERE id = ?",
                (
                    status,
                    provider_sid,
                    (early[1] if early and early[1] else error[:200]) or None,
                    early[0] if early else None,
                    message_id,
                ),
            )

    def retry_outbound(self, message_id: int, delay_seconds: float, error: str) -> None:
        with self._transaction() as db:
            db.execute(
                "UPDATE outbound SET status = 'pending', next_attempt_at = ?, error = ? WHERE id = ?",
                (self._clock() + delay_seconds, error[:200], message_id),
            )

    def record_delivery(self, provider_sid: str, delivery: str, error_code: str | None) -> str:
        """Apply a Twilio status callback, only ever moving a message forward.

        Returns "applied", "stale" (an older or equal status arrived late), "early" (kept until
        the send records its SID) or "ignored" (an unrecognised status). Callbacks may arrive
        more than once and in any order.
        """
        rank = DELIVERY_RANK.get(delivery)
        if rank is None:
            return "ignored"
        error = f"twilio {error_code}" if error_code else None
        with self._transaction() as db:
            row = db.execute("SELECT delivery FROM outbound WHERE provider_sid = ?", (provider_sid,)).fetchone()
            if row is None:
                early = db.execute(
                    "SELECT delivery FROM delivery_events WHERE provider_sid = ?", (provider_sid,)
                ).fetchone()
                if early is None or DELIVERY_RANK.get(early[0], 0) < rank:
                    db.execute(
                        "INSERT OR REPLACE INTO delivery_events VALUES (?, ?, ?, ?)",
                        (provider_sid, delivery, error, self._clock()),
                    )
                return "early"
            if DELIVERY_RANK.get(row[0] or "", 0) >= rank:
                return "stale"
            db.execute(
                "UPDATE outbound SET delivery = ?, error = coalesce(?, error) WHERE provider_sid = ?",
                (delivery, error, provider_sid),
            )
            return "applied"

    def outbound_for(self, message_sid: str) -> list[tuple[str, str | None, str | None, int, str | None]]:
        """(status, provider_sid, error, attempts, delivery) for each reply to a message."""
        with self._lock:
            return self._db.execute(
                "SELECT status, provider_sid, error, attempts, delivery FROM outbound "
                "WHERE message_sid = ? ORDER BY id",
                (message_sid,),
            ).fetchall()

    # Write journal -----------------------------------------------------------------
    #
    # confirmed ─start_operation─▶ executing ─▶ done | failed | unknown
    # A confirmed intent has never touched GitHub and may be attempted once. Anything that
    # reached executing is never attempted again; its outcome is settled or reconciled.

    def record_intent(
        self,
        op_id: str,
        sender_key: str,
        action: str,
        repo: str,
        number: int | None,
        fingerprint: str,
        command: dict,
    ) -> Operation | None:
        """Durably record an approved write before any attempt; returns the earlier record if one exists."""
        now = self._clock()
        with self._transaction() as db:
            inserted = db.execute(
                "INSERT OR IGNORE INTO operations VALUES (?, ?, ?, ?, ?, ?, 'confirmed', NULL, ?, ?, ?)",
                (
                    op_id,
                    sender_key,
                    action,
                    repo,
                    number,
                    fingerprint,
                    now,
                    now,
                    self._sealer.seal(command, sender_key),
                ),
            ).rowcount
            if inserted:
                return None
            row = db.execute(f"SELECT {OPERATION_COLUMNS} FROM operations WHERE op_id = ?", (op_id,)).fetchone()  # noqa: S608
        return Operation(*row)

    def intent(self, op_id: str) -> dict | None:
        """The sealed command of a write that was approved but never attempted."""
        with self._lock:
            row = self._db.execute(
                "SELECT sender_key, command FROM operations WHERE op_id = ? AND status = 'confirmed'", (op_id,)
            ).fetchone()
        return self._sealer.open(row[1], row[0]) if row and row[1] else None

    def start_operation(self, op_id: str) -> bool:
        """Move an intent to executing, exactly once; False if it was already attempted or settled."""
        with self._transaction() as db:
            return (
                db.execute(
                    "UPDATE operations SET status = 'executing', command = NULL, updated_at = ? "
                    "WHERE op_id = ? AND status = 'confirmed'",
                    (self._clock(), op_id),
                ).rowcount
                == 1
            )

    def finish_operation(self, op_id: str, status: str, url: str | None = None) -> None:
        with self._transaction() as db:
            db.execute(
                "UPDATE operations SET status = ?, url = coalesce(?, url), command = NULL, updated_at = ? "
                "WHERE op_id = ?",
                (status, url, self._clock(), op_id),
            )

    def operation(self, op_id: str) -> Operation | None:
        with self._lock:
            row = self._db.execute(f"SELECT {OPERATION_COLUMNS} FROM operations WHERE op_id = ?", (op_id,)).fetchone()  # noqa: S608
        return Operation(*row) if row else None

    def similar_operation(self, sender_key: str, fingerprint: str, within_seconds: float) -> Operation | None:
        """The latest write with the same content that is done or not yet settled, to warn before a duplicate."""
        with self._lock:
            row = self._db.execute(
                f"SELECT {OPERATION_COLUMNS} FROM operations "  # noqa: S608
                "WHERE sender_key = ? AND fingerprint = ? "
                "AND status IN ('done', 'observed', 'unknown', 'executing', 'confirmed') "
                "AND created_at >= ? ORDER BY created_at DESC LIMIT 1",
                (sender_key, fingerprint, self._clock() - within_seconds),
            ).fetchone()
        return Operation(*row) if row else None

    def unresolved_operations(self) -> list[Operation]:
        with self._lock:
            rows = self._db.execute(
                f"SELECT {OPERATION_COLUMNS} FROM operations "  # noqa: S608
                "WHERE status IN ('confirmed', 'executing', 'unknown') ORDER BY created_at"
            ).fetchall()
        return [Operation(*row) for row in rows]

    # Health --------------------------------------------------------------------------

    def health(self) -> dict[str, float | int]:
        """Counts and ages an operator or a readiness probe needs; no identifiers."""
        now = self._clock()
        with self._lock:
            inbound = self._db.execute(
                "SELECT count(*), min(received_at) FROM inbound WHERE status IN ('queued', 'processing')"
            ).fetchone()
            replies = self._db.execute(
                "SELECT count(*), min(created_at) FROM outbound WHERE status IN ('pending', 'sending')"
            ).fetchone()
            unknown_replies = self._db.execute("SELECT count(*) FROM outbound WHERE status = 'unknown'").fetchone()[0]
            writes = dict(
                self._db.execute(
                    "SELECT status, count(*) FROM operations WHERE status IN ('confirmed', 'executing', 'unknown') "
                    "GROUP BY status"
                ).fetchall()
            )
        return {
            "inbound_waiting": inbound[0],
            "oldest_inbound_seconds": round(now - inbound[1], 1) if inbound[1] else 0,
            "replies_waiting": replies[0],
            "oldest_reply_seconds": round(now - replies[1], 1) if replies[1] else 0,
            "replies_unknown": unknown_replies,
            "writes_approved_unattempted": writes.get("confirmed", 0),
            "writes_in_progress": writes.get("executing", 0),
            "writes_unknown": writes.get("unknown", 0),
        }

    # Fixed-window limits -------------------------------------------------------------

    def consume(self, key: str, window_seconds: int, amount: int = 1) -> int:
        """Add amount (negative to refund) to the current window of key and return the new total."""
        window = int(self._clock() // window_seconds) * window_seconds
        with self._transaction() as db:
            row = db.execute("SELECT window_start, count FROM counters WHERE key = ?", (key,)).fetchone()
            total = amount if not row or row[0] != window else row[1] + amount
            total = max(total, 0)
            db.execute("INSERT OR REPLACE INTO counters VALUES (?, ?, ?)", (key, window, total))
        return total

    def peek(self, key: str, window_seconds: int) -> int:
        window = int(self._clock() // window_seconds) * window_seconds
        with self._lock:
            row = self._db.execute("SELECT window_start, count FROM counters WHERE key = ?", (key,)).fetchone()
        return row[1] if row and row[0] == window else 0

    # Confirmations -------------------------------------------------------------------

    def set_pending(
        self,
        sender_key: str,
        code: str,
        command: dict,
        ttl_seconds: int,
        *,
        action: str,
        repo: str,
        number: int | None,
        fingerprint: str,
    ) -> bool:
        """Store the change awaiting confirmation; True if it replaced an earlier, still-valid one."""
        with self._transaction() as db:
            replaced = db.execute(
                "DELETE FROM pending WHERE sender_key = ? AND expires_at >= ?", (sender_key, self._clock())
            ).rowcount
            db.execute(
                "INSERT OR REPLACE INTO pending VALUES (?, ?, ?, ?, 0, ?, ?, ?, ?)",
                (
                    sender_key,
                    self._mac("code", sender_key, code),
                    self._sealer.seal(command, sender_key),
                    self._clock() + ttl_seconds,
                    action,
                    repo,
                    number,
                    fingerprint,
                ),
            )
        return replaced == 1

    def confirm_pending(self, sender_key: str, code: str, max_attempts: int, op_id: str) -> tuple[str, dict | None]:
        """Atomically check the code and turn the pending change into a durable write intent.

        Returns ("ok", command), or ("none" | "expired" | "wrong" | "locked", None). The same
        transaction that consumes the pending change journals it as operation op_id in state
        "confirmed", so a crash at any later point leaves the approval on record. Recording
        the approval does not guarantee execution: the intent is attempted at most once and
        may still fail or end unknown. A command is returned at most once, and only for the
        code it was created with.
        """
        with self._transaction() as db:
            row = db.execute(
                "SELECT code_mac, command, expires_at, attempts, action, repo, number, fingerprint "
                "FROM pending WHERE sender_key = ?",
                (sender_key,),
            ).fetchone()
            if row is None:
                return "none", None
            code_mac, sealed, expires_at, attempts, action, repo, number, fingerprint = row
            if expires_at < self._clock():
                db.execute("DELETE FROM pending WHERE sender_key = ?", (sender_key,))
                return "expired", None
            if not hmac.compare_digest(code_mac, self._mac("code", sender_key, code)):
                if attempts + 1 >= max_attempts:
                    db.execute("DELETE FROM pending WHERE sender_key = ?", (sender_key,))
                    return "locked", None
                db.execute("UPDATE pending SET attempts = attempts + 1 WHERE sender_key = ?", (sender_key,))
                return "wrong", None
            now = self._clock()
            db.execute(
                "INSERT INTO operations VALUES (?, ?, ?, ?, ?, ?, 'confirmed', NULL, ?, ?, ?)",
                (op_id, sender_key, action, repo, number, fingerprint, now, now, sealed),
            )
            db.execute("DELETE FROM pending WHERE sender_key = ?", (sender_key,))
        return "ok", self._sealer.open(sealed, sender_key)

    def drop_pending(self, sender_key: str) -> bool:
        with self._transaction() as db:
            return (
                db.execute(
                    "DELETE FROM pending WHERE sender_key = ? AND expires_at >= ?", (sender_key, self._clock())
                ).rowcount
                == 1
            )

    # Conversation context ------------------------------------------------------------

    def default_repo(self, sender_key: str) -> str | None:
        with self._lock:
            row = self._db.execute("SELECT default_repo FROM context WHERE sender_key = ?", (sender_key,)).fetchone()
        return row[0] if row else None

    def set_default_repo(self, sender_key: str, repo: str) -> None:
        with self._transaction() as db:
            db.execute(
                "INSERT INTO context (sender_key, default_repo) VALUES (?, ?) "
                "ON CONFLICT(sender_key) DO UPDATE SET default_repo = excluded.default_repo",
                (sender_key, repo),
            )

    def set_pages(self, sender_key: str, pages: list[str], ttl_seconds: int) -> None:
        sealed = self._sealer.seal(pages, sender_key) if pages else None
        expires = self._clock() + ttl_seconds if pages else None
        with self._transaction() as db:
            db.execute(
                "INSERT INTO context (sender_key, pages, pages_expire_at) VALUES (?, ?, ?) ON CONFLICT(sender_key) "
                "DO UPDATE SET pages = excluded.pages, pages_expire_at = excluded.pages_expire_at",
                (sender_key, sealed, expires),
            )

    def next_page(self, sender_key: str) -> str | None:
        with self._transaction() as db:
            row = db.execute(
                "SELECT pages, pages_expire_at FROM context WHERE sender_key = ?", (sender_key,)
            ).fetchone()
            if not row or not row[0] or row[1] < self._clock():
                return None
            pages = self._sealer.open(row[0], sender_key)
            rest = self._sealer.seal(pages[1:], sender_key) if len(pages) > 1 else None
            db.execute("UPDATE context SET pages = ? WHERE sender_key = ?", (rest, sender_key))
        return pages[0]

    # Opt-outs ------------------------------------------------------------------------

    def set_opted_out(self, sender_key: str, opted_out: bool) -> None:
        with self._transaction() as db:
            if opted_out:
                db.execute("INSERT OR REPLACE INTO opt_outs VALUES (?, ?)", (sender_key, self._clock()))
            else:
                db.execute("DELETE FROM opt_outs WHERE sender_key = ?", (sender_key,))

    def is_opted_out(self, sender_key: str) -> bool:
        with self._lock:
            return self._db.execute("SELECT 1 FROM opt_outs WHERE sender_key = ?", (sender_key,)).fetchone() is not None

    # Audit ---------------------------------------------------------------------------

    def audit(
        self,
        *,
        sender_key: str,
        action: str,
        outcome: str,
        message_sid: str | None = None,
        alias: str | None = None,
        repo: str | None = None,
        detail: str | None = None,
        url: str | None = None,
    ) -> None:
        with self._transaction() as db:
            db.execute(
                "INSERT INTO audit (at, message_sid, sender_key, alias, action, repo, outcome, detail, url) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (self._clock(), message_sid, sender_key, alias, action, repo, outcome, (detail or "")[:500], url),
            )

    def audit_log(self, limit: int = 100) -> list[AuditEntry]:
        with self._lock:
            rows = self._db.execute(
                "SELECT at, message_sid, sender_key, alias, action, repo, outcome, detail, url "
                "FROM audit ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [AuditEntry(*row) for row in rows]

    # Retention -----------------------------------------------------------------------

    def sweep(self, retention_days: int, audit_retention_days: int) -> dict[str, int]:
        """Delete what is no longer needed. Unsettled writes are kept as long as the audit log."""
        now = self._clock()
        cutoff, audit_cutoff = now - retention_days * 86400, now - audit_retention_days * 86400
        with self._transaction() as db:
            counts = {
                "pending": db.execute("DELETE FROM pending WHERE expires_at < ?", (now,)).rowcount,
                "pages": db.execute(
                    "UPDATE context SET pages = NULL, pages_expire_at = NULL WHERE pages_expire_at < ?", (now,)
                ).rowcount,
                "inbound": db.execute(
                    "DELETE FROM inbound WHERE status IN ('done', 'failed') AND received_at < ?", (cutoff,)
                ).rowcount,
                "outbound": db.execute(
                    "DELETE FROM outbound WHERE status IN (?, ?, ?) AND created_at < ?", (*SETTLED_OUTBOUND, cutoff)
                ).rowcount,
                "delivery_events": db.execute("DELETE FROM delivery_events WHERE at < ?", (now - 86400,)).rowcount,
                "operations": db.execute(
                    "DELETE FROM operations WHERE (status IN ('done', 'failed') AND created_at < ?) OR created_at < ?",
                    (cutoff, audit_cutoff),
                ).rowcount,
                "audit": db.execute("DELETE FROM audit WHERE at < ?", (audit_cutoff,)).rowcount,
                "counters": db.execute("DELETE FROM counters WHERE window_start < ?", (now - 2 * 86400,)).rowcount,
            }
        return counts
