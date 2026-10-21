"""Who may text this service, and what each sender may read or change.

The directory is an operator-owned TOML file. Phone numbers are matched exactly in
E.164 form; repository grants are explicit patterns. Nothing is writable unless a
grant says so.

    public_read = false            # unknown senders get nothing (the default)

    [[users]]
    phone = "+15551234567"
    alias = "ana"                  # shown on GitHub; the number never is
    read = ["*"]
    write = ["my-org/field-reports", "my-org/docs-*"]
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from pathlib import Path

from git_sms.config import ConfigError

E164 = re.compile(r"^\+[1-9]\d{6,14}$")
ALIAS = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,38}$")
PATTERN = re.compile(r"^(\*|[A-Za-z0-9*][A-Za-z0-9-*]{0,38}/[A-Za-z0-9._*-]{1,100})$")
GUEST_ALIAS = "guest"


def _matches(patterns: tuple[str, ...], repo: str) -> bool:
    repo = repo.lower()
    return any(pattern == "*" or fnmatchcase(repo, pattern.lower()) for pattern in patterns)


@dataclass(frozen=True)
class User:
    phone: str
    alias: str
    read: tuple[str, ...] = ()
    write: tuple[str, ...] = ()
    guest: bool = False

    def can_read(self, repo: str) -> bool:
        return _matches(self.read, repo) or self.can_write(repo)

    def can_write(self, repo: str) -> bool:
        return not self.guest and _matches(self.write, repo)


@dataclass(frozen=True)
class Directory:
    users: dict[str, User] = field(default_factory=dict)
    public_read: bool = False

    def lookup(self, phone: str) -> User | None:
        user = self.users.get(phone)
        if user:
            return user
        if self.public_read and E164.match(phone):
            return User(phone=phone, alias=GUEST_ALIAS, read=("*",), guest=True)
        return None

    @classmethod
    def from_toml(cls, text: str) -> Directory:
        try:
            data = tomllib.loads(text)
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(f"users file is not valid TOML: {exc}") from exc

        public_read = data.get("public_read", False)
        if not isinstance(public_read, bool):
            raise ConfigError("public_read must be true or false")
        entries = data.get("users", [])
        if not isinstance(entries, list):
            raise ConfigError("users must be an array of tables")

        users: dict[str, User] = {}
        aliases: set[str] = set()
        for index, entry in enumerate(entries, start=1):
            if not isinstance(entry, dict):
                raise ConfigError(f"user {index} must be a table")
            phone, alias = entry.get("phone"), entry.get("alias")
            if not isinstance(phone, str) or not E164.match(phone):
                raise ConfigError(f"user {index}: phone must be E.164, like +15551234567")
            if not isinstance(alias, str) or not ALIAS.match(alias) or alias.lower() == GUEST_ALIAS:
                raise ConfigError(f"user {index}: alias must be 1-39 letters, digits, '.', '_' or '-'")
            if phone in users:
                raise ConfigError(f"user {index}: phone is listed twice")
            if alias.lower() in aliases:
                raise ConfigError(f"user {index}: alias {alias!r} is listed twice")
            grants = {}
            for kind in ("read", "write"):
                patterns = entry.get(kind, [])
                if not isinstance(patterns, list) or not all(isinstance(p, str) and PATTERN.match(p) for p in patterns):
                    raise ConfigError(f"user {index}: {kind} must be a list of owner/repo patterns or '*'")
                grants[kind] = tuple(patterns)
            unknown = set(entry) - {"phone", "alias", "read", "write"}
            if unknown:
                raise ConfigError(f"user {index}: unknown keys {sorted(unknown)}")
            users[phone] = User(phone=phone, alias=alias, read=grants["read"], write=grants["write"])
            aliases.add(alias.lower())
        return cls(users=users, public_read=public_read)

    @classmethod
    def load(cls, path: str | None) -> Directory:
        if not path:
            return cls()
        try:
            return cls.from_toml(Path(path).read_text())
        except OSError as exc:
            raise ConfigError(f"cannot read users file: {exc.strerror}") from exc
