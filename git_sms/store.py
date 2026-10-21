"""Durable state in one SQLite file: intake queue, write journal, outbound ledger,
limits, confirmations, context, opt-outs and audit.

Senders are identified by keyed hashes (HMAC-SHA256 with GIT_SMS_SECRET_KEY). What must
be kept briefly in recoverable form (the queued message with its number, reply text,
pending changes, MORE pages) is sealed with AES-GCM under a key derived from the same
secret, and is deleted as soon as it is no longer needed. Nothing in the file reveals a
phone number or message content without the secret, which never lives in the database.
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

SCHEMA_VERSION = 1
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
    status TEXT NOT NULL,             -- pending, sending, accepted, failed, unknown, then Twilio's statuses
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at REAL NOT NULL,
    segments INTEGER NOT NULL,
    provider_sid TEXT UNIQUE,
    error TEXT,
    payload BLOB                      -- sealed; cleared once the provider accepted or refused it
);
CREATE INDEX outbound_status ON outbound (status, next_attempt_at);
CREATE TABLE operations (
    op_id TEXT PRIMARY KEY,           -- derived from the MessageSid that triggered the write
    sender_key TEXT NOT NULL,
    action TEXT NOT NULL,
    repo TEXT NOT NULL,
    number INTEGER,
    fingerprint TEXT NOT NULL,
    status TEXT NOT NULL,             -- executing, done, failed, unknown
    url TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
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
    attempts INTEGER NOT NULL DEFAULT 0
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
TERMINAL_OUTBOUND = ("accepted", "failed", "unknown", "sent", "delivered", "undelivered", "read", "canceled")


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
        """Public, unguessable id for the write a message triggered (embedded in GitHub bodies)."""
        return self._mac("op", message_sid)[:20]

    # Intake queue ------------------------------------------------------------------

    def enqueue_inbound(self, message_sid: str, sender_key: str, payload: dict) -> bool:
        """Durably queue a message before it is acknowledged; False means it was seen before."""
        with self._transaction() as db:
            cursor = db.execute(
                "INSERT OR IGNORE INTO inbound (message_sid, sender_key, received_at, status, payload) "
                "VALUES (?, ?, ?, 'queued', ?)",
                (message_sid, sender_key, self._clock(), self._sealer.seal(payload, sender_key)),
            )
            return cursor.rowcount == 1

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
        with self._transaction() as db:
            row = db.execute(
                "SELECT id, message_sid, sender_key, payload, segments, attempts FROM outbound "
                "WHERE status = 'pending' AND next_attempt_at <= ? ORDER BY id LIMIT 1",
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
            db.execute(
                "UPDATE outbound SET status = ?, provider_sid = ?, error = ?, payload = NULL WHERE id = ?",
                (status, provider_sid, error[:200] or None, message_id),
            )

    def retry_outbound(self, message_id: int, delay_seconds: float, error: str) -> None:
        with self._transaction() as db:
            db.execute(
                "UPDATE outbound SET status = 'pending', next_attempt_at = ?, error = ? WHERE id = ?",
                (self._clock() + delay_seconds, error[:200], message_id),
            )

    def record_delivery(self, provider_sid: str, status: str, error_code: str | None) -> bool:
        """Apply a Twilio status callback; False if the message is not one of ours."""
        with self._transaction() as db:
            return (
                db.execute(
                    "UPDATE outbound SET status = ?, error = coalesce(?, error) WHERE provider_sid = ?",
                    (status, f"twilio {error_code}" if error_code else None, provider_sid),
                ).rowcount
                == 1
            )

    def outbound_for(self, message_sid: str) -> list[tuple[str, str | None, str | None, int]]:
        with self._lock:
            return self._db.execute(
                "SELECT status, provider_sid, error, attempts FROM outbound WHERE message_sid = ? ORDER BY id",
                (message_sid,),
            ).fetchall()

    # Write journal -----------------------------------------------------------------

    def begin_operation(
        self, op_id: str, sender_key: str, action: str, repo: str, number: int | None, fingerprint: str
    ) -> Operation | None:
        """Journal a write before it is attempted; returns the earlier record if this op already ran."""
        now = self._clock()
        with self._transaction() as db:
            inserted = db.execute(
                "INSERT OR IGNORE INTO operations VALUES (?, ?, ?, ?, ?, ?, 'executing', NULL, ?, ?)",
                (op_id, sender_key, action, repo, number, fingerprint, now, now),
            ).rowcount
            if inserted:
                return None
            row = db.execute(
                "SELECT op_id, action, repo, number, status, url, created_at FROM operations WHERE op_id = ?", (op_id,)
            ).fetchone()
        return Operation(*row)

    def finish_operation(self, op_id: str, status: str, url: str | None = None) -> None:
        with self._transaction() as db:
            db.execute(
                "UPDATE operations SET status = ?, url = coalesce(?, url), updated_at = ? WHERE op_id = ?",
                (status, url, self._clock(), op_id),
            )

    def operation(self, op_id: str) -> Operation | None:
        with self._lock:
            row = self._db.execute(
                "SELECT op_id, action, repo, number, status, url, created_at FROM operations WHERE op_id = ?", (op_id,)
            ).fetchone()
        return Operation(*row) if row else None

    def similar_operation(self, sender_key: str, fingerprint: str, within_seconds: float) -> Operation | None:
        """The latest done or unresolved write with the same content, to warn before a duplicate."""
        with self._lock:
            row = self._db.execute(
                "SELECT op_id, action, repo, number, status, url, created_at FROM operations "
                "WHERE sender_key = ? AND fingerprint = ? AND status IN ('done', 'unknown', 'executing') "
                "AND created_at >= ? ORDER BY created_at DESC LIMIT 1",
                (sender_key, fingerprint, self._clock() - within_seconds),
            ).fetchone()
        return Operation(*row) if row else None

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

    def set_pending(self, sender_key: str, code: str, command: dict, ttl_seconds: int) -> bool:
        """Store the change awaiting confirmation; True if it replaced an earlier one."""
        with self._transaction() as db:
            replaced = db.execute(
                "DELETE FROM pending WHERE sender_key = ? AND expires_at >= ?", (sender_key, self._clock())
            ).rowcount
            db.execute(
                "INSERT OR REPLACE INTO pending VALUES (?, ?, ?, ?, 0)",
                (
                    sender_key,
                    self._mac("code", sender_key, code),
                    self._sealer.seal(command, sender_key),
                    self._clock() + ttl_seconds,
                ),
            )
        return replaced == 1

    def confirm_pending(self, sender_key: str, code: str, max_attempts: int) -> tuple[str, dict | None]:
        """Atomically check and take the pending change.

        Returns ("ok", command), ("none" | "expired" | "wrong" | "locked", None). A command
        is returned at most once, and only for the code it was created with.
        """
        with self._transaction() as db:
            row = db.execute(
                "SELECT code_mac, command, expires_at, attempts FROM pending WHERE sender_key = ?", (sender_key,)
            ).fetchone()
            if row is None:
                return "none", None
            code_mac, sealed, expires_at, attempts = row
            if expires_at < self._clock():
                db.execute("DELETE FROM pending WHERE sender_key = ?", (sender_key,))
                return "expired", None
            if not hmac.compare_digest(code_mac, self._mac("code", sender_key, code)):
                if attempts + 1 >= max_attempts:
                    db.execute("DELETE FROM pending WHERE sender_key = ?", (sender_key,))
                    return "locked", None
                db.execute("UPDATE pending SET attempts = attempts + 1 WHERE sender_key = ?", (sender_key,))
                return "wrong", None
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
        """Delete what is no longer needed. Unresolved writes are kept as long as the audit log."""
        now = self._clock()
        cutoff, audit_cutoff = now - retention_days * 86400, now - audit_retention_days * 86400
        terminal = ",".join("?" * len(TERMINAL_OUTBOUND))
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
                    f"DELETE FROM outbound WHERE status IN ({terminal}) AND created_at < ?",  # noqa: S608
                    (*TERMINAL_OUTBOUND, cutoff),
                ).rowcount,
                "operations": db.execute(
                    "DELETE FROM operations WHERE (status IN ('done', 'failed') AND created_at < ?) OR created_at < ?",
                    (cutoff, audit_cutoff),
                ).rowcount,
                "audit": db.execute("DELETE FROM audit WHERE at < ?", (audit_cutoff,)).rowcount,
                "counters": db.execute("DELETE FROM counters WHERE window_start < ?", (now - 2 * 86400,)).rowcount,
            }
        return counts
