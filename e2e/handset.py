"""Talk to an Android/Termux handset through its git-sms-gate forced-command SSH key.

The gate (e2e/phone/git-sms-gate.sh) can only text allowlisted numbers and only return the
conversation with them, so this client never sees anything else on the phone. Termux:API's
sender filter returns both directions of that conversation (it ignores `-t inbox` when `-f` is
given), so inbox() keeps only messages the handset received: a gateway must never read its
own replies as new commands.
"""

from __future__ import annotations

import base64
import hashlib
import json
import subprocess

from git_sms.worker import SendError


class GateError(RuntimeError):
    pass


class Handset:
    def __init__(self, host: str, key: str, timeout: float = 60.0):
        self.host = host
        self.key = key
        self.timeout = timeout

    def _run(self, command: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes", "-i", self.key, self.host, command],
            capture_output=True,
            text=True,
            timeout=self.timeout,
            check=False,
        )

    def ping(self) -> dict[str, str]:
        result = self._run("ping")
        if result.returncode != 0:
            raise GateError(f"gate unreachable (ssh exit {result.returncode})")
        return dict(line.split(": ", 1) for line in result.stdout.splitlines() if ": " in line)

    def send(self, number: str, body: str) -> None:
        encoded = base64.b64encode(body.encode()).decode()
        result = self._run(f"send {number} {encoded}")
        if result.returncode == 255:
            raise SendError("ssh connection failed", retry=True)  # never reached the phone
        if result.returncode != 0 or result.stdout.strip() != "sent":
            raise SendError(f"gate: {result.stdout.strip()[:120]}")

    def inbox(self, number: str, limit: int = 20) -> list[dict]:
        result = self._run(f"inbox {number} {limit}")
        if result.returncode != 0:
            raise GateError(f"inbox: {result.stdout.strip()[:120] or result.returncode}")
        messages = [m for m in json.loads(result.stdout or "[]") if m.get("type") == "inbox"]
        for message in messages:
            message["key"] = hashlib.sha256(
                f"{message.get('_id')}|{message.get('received')}|{message.get('body')}".encode()
            ).hexdigest()[:32]
        return messages


class HandsetSender:
    """A Git SMS reply sender that texts from the handset's own SIM."""

    def __init__(self, handset: Handset):
        self.handset = handset

    def send(self, *, to: str, from_: str, body: str, status_callback: str | None = None) -> str | None:
        self.handset.send(to, body)
        return None
