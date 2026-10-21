"""Durable state in one SQLite file: idempotency, limits, confirmations, context, audit.

Senders are stored only as keyed hashes (HMAC-SHA256 with GIT_SMS_SECRET_KEY), so the
database never holds a phone number. Every reply answers an inbound message that
carries the number, so nothing ever needs to be reversed.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

SCHEMA = """
CREATE TABLE IF NOT EXISTS inbound (
    message_sid TEXT PRIMARY KEY,
    sender_key TEXT NOT NULL,
    received_at REAL NOT NULL,
    status TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS counters (
    key TEXT PRIMARY KEY,
    window_start INTEGER NOT NULL,
    count INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS pending (
    sender_key TEXT PRIMARY KEY,
    code TEXT NOT NULL,
    command TEXT NOT NULL,
    expires_at REAL NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS context (
    sender_key TEXT PRIMARY KEY,
    default_repo TEXT,
    pages TEXT NOT NULL DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS opt_outs (
    sender_key TEXT PRIMARY KEY,
    at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS audit (
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


@dataclass(frozen=True)
class Pending:
    code: str
    command: dict
    expires_at: float
    attempts: int


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


class Store:
    def __init__(self, path: str, secret_key: str, clock: Callable[[], float] = time.time):
        self._clock = clock
        self._secret = secret_key.encode()
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        if path != ":memory:":
            self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA busy_timeout=5000")
        self._db.executescript(SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def now(self) -> float:
        return self._clock()

    def sender_key(self, phone: str) -> str:
        return hmac.new(self._secret, phone.encode(), hashlib.sha256).hexdigest()[:32]

    # Idempotency -----------------------------------------------------------------

    def claim_inbound(self, message_sid: str, sender_key: str) -> bool:
        """Record a MessageSid; False means it was already seen and must not run again."""
        with self._lock:
            cursor = self._db.execute(
                "INSERT OR IGNORE INTO inbound VALUES (?, ?, ?, 'received')",
                (message_sid, sender_key, self._clock()),
            )
            return cursor.rowcount == 1

    def finish_inbound(self, message_sid: str, status: str) -> None:
        with self._lock:
            self._db.execute("UPDATE inbound SET status = ? WHERE message_sid = ?", (status, message_sid))

    def inbound_status(self, message_sid: str) -> str | None:
        with self._lock:
            row = self._db.execute("SELECT status FROM inbound WHERE message_sid = ?", (message_sid,)).fetchone()
        return row[0] if row else None

    # Fixed-window limits ---------------------------------------------------------

    def consume(self, key: str, window_seconds: int, amount: int = 1) -> int:
        """Add amount to the current window of key and return the new total."""
        window = int(self._clock() // window_seconds) * window_seconds
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                row = self._db.execute("SELECT window_start, count FROM counters WHERE key = ?", (key,)).fetchone()
                total = amount if not row or row[0] != window else row[1] + amount
                self._db.execute("INSERT OR REPLACE INTO counters VALUES (?, ?, ?)", (key, window, total))
                self._db.execute("COMMIT")
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
        return total

    def peek(self, key: str, window_seconds: int) -> int:
        window = int(self._clock() // window_seconds) * window_seconds
        with self._lock:
            row = self._db.execute("SELECT window_start, count FROM counters WHERE key = ?", (key,)).fetchone()
        return row[1] if row and row[0] == window else 0

    # Confirmations ---------------------------------------------------------------

    def set_pending(self, sender_key: str, code: str, command: dict, ttl_seconds: int) -> None:
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO pending VALUES (?, ?, ?, ?, 0)",
                (sender_key, code, json.dumps(command), self._clock() + ttl_seconds),
            )

    def get_pending(self, sender_key: str) -> Pending | None:
        with self._lock:
            row = self._db.execute(
                "SELECT code, command, expires_at, attempts FROM pending WHERE sender_key = ?", (sender_key,)
            ).fetchone()
        return Pending(row[0], json.loads(row[1]), row[2], row[3]) if row else None

    def take_pending(self, sender_key: str) -> Pending | None:
        """Remove and return the pending change, so it can execute at most once."""
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                row = self._db.execute(
                    "SELECT code, command, expires_at, attempts FROM pending WHERE sender_key = ?", (sender_key,)
                ).fetchone()
                self._db.execute("DELETE FROM pending WHERE sender_key = ?", (sender_key,))
                self._db.execute("COMMIT")
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
        return Pending(row[0], json.loads(row[1]), row[2], row[3]) if row else None

    def fail_pending(self, sender_key: str) -> int:
        with self._lock:
            self._db.execute("UPDATE pending SET attempts = attempts + 1 WHERE sender_key = ?", (sender_key,))
            row = self._db.execute("SELECT attempts FROM pending WHERE sender_key = ?", (sender_key,)).fetchone()
        return row[0] if row else 0

    def drop_pending(self, sender_key: str) -> bool:
        with self._lock:
            return self._db.execute("DELETE FROM pending WHERE sender_key = ?", (sender_key,)).rowcount == 1

    # Conversation context --------------------------------------------------------

    def default_repo(self, sender_key: str) -> str | None:
        with self._lock:
            row = self._db.execute("SELECT default_repo FROM context WHERE sender_key = ?", (sender_key,)).fetchone()
        return row[0] if row else None

    def set_default_repo(self, sender_key: str, repo: str) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO context (sender_key, default_repo) VALUES (?, ?) "
                "ON CONFLICT(sender_key) DO UPDATE SET default_repo = excluded.default_repo",
                (sender_key, repo),
            )

    def set_pages(self, sender_key: str, pages: list[str]) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO context (sender_key, pages) VALUES (?, ?) "
                "ON CONFLICT(sender_key) DO UPDATE SET pages = excluded.pages",
                (sender_key, json.dumps(pages)),
            )

    def next_page(self, sender_key: str) -> str | None:
        with self._lock:
            row = self._db.execute("SELECT pages FROM context WHERE sender_key = ?", (sender_key,)).fetchone()
            pages = json.loads(row[0]) if row else []
            if not pages:
                return None
            self._db.execute("UPDATE context SET pages = ? WHERE sender_key = ?", (json.dumps(pages[1:]), sender_key))
        return pages[0]

    # Opt-outs --------------------------------------------------------------------

    def set_opted_out(self, sender_key: str, opted_out: bool) -> None:
        with self._lock:
            if opted_out:
                self._db.execute("INSERT OR REPLACE INTO opt_outs VALUES (?, ?)", (sender_key, self._clock()))
            else:
                self._db.execute("DELETE FROM opt_outs WHERE sender_key = ?", (sender_key,))

    def is_opted_out(self, sender_key: str) -> bool:
        with self._lock:
            return self._db.execute("SELECT 1 FROM opt_outs WHERE sender_key = ?", (sender_key,)).fetchone() is not None

    # Audit -----------------------------------------------------------------------

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
        with self._lock:
            self._db.execute(
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
