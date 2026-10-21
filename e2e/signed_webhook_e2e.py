#!/usr/bin/env python3
"""End-to-end check of a running Git SMS server against a real (disposable) GitHub repo.

It plays Twilio: each message is POSTed with a genuine X-Twilio-Signature, so the
server's authentication, idempotency, authorisation, confirmation and GitHub write
paths all run for real. Replies are read from the development outbox
(GIT_SMS_SENDER=console, GIT_SMS_DEV_OUTBOX). Every effect is then verified
independently through the GitHub REST API, not through the server's own replies.

This proves everything except the carrier and Twilio legs, which need a real
number (see docs/TESTING.md). It never needs or prints a real phone number.

Environment:
  E2E_TWILIO_AUTH_TOKEN, E2E_TWILIO_ACCOUNT_SID  the values the server was started with
  GITHUB_TOKEN                                   used only to verify results
Usage:
  e2e/signed_webhook_e2e.py --url http://127.0.0.1:8765 --repo owner/sandbox \\
      --outbox /path/outbox.jsonl --evidence /path/evidence.json
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import sys
import time
from pathlib import Path

import httpx
from twilio.request_validator import RequestValidator

SENDER = "+15550100001"  # fictional; must be the writer in the server's users file
OUTSIDER = "+15550100009"  # fictional; must not be in the users file
OUR_NUMBER = "+15550100100"


class Harness:
    def __init__(self, args: argparse.Namespace):
        self.url = args.url.rstrip("/") + "/twilio/sms"
        self.signed_url = (args.public_url or args.url).rstrip("/") + "/twilio/sms"
        self.validator = RequestValidator(os.environ["E2E_TWILIO_AUTH_TOKEN"])
        self.account = os.environ["E2E_TWILIO_ACCOUNT_SID"]
        self.outbox = Path(args.outbox)
        self.repo = args.repo
        self.http = httpx.Client(timeout=30)
        self.github = httpx.Client(
            base_url="https://api.github.com",
            timeout=30,
            headers={
                "Authorization": f"Bearer {os.environ['GITHUB_TOKEN']}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        self.steps: list[dict] = []
        self.failures = 0

    def form(self, body: str, sid: str, sender: str) -> dict[str, str]:
        return {
            "MessageSid": sid,
            "SmsMessageSid": sid,
            "AccountSid": self.account,
            "From": sender,
            "To": OUR_NUMBER,
            "Body": body,
            "NumMedia": "0",
            "ApiVersion": "2010-04-01",
        }

    def post(self, params: dict[str, str], signature: str | None) -> httpx.Response:
        headers = {"X-Twilio-Signature": signature} if signature else {}
        return self.http.post(self.url, data=params, headers=headers)

    def replies_since(self, offset: int, wait: float = 20.0) -> list[str]:
        deadline = time.time() + wait
        while time.time() < deadline:
            lines = self.outbox.read_text().splitlines() if self.outbox.exists() else []
            if len(lines) > offset:
                time.sleep(0.3)
                lines = self.outbox.read_text().splitlines()
                return [json.loads(line)["body"] for line in lines[offset:]]
            time.sleep(0.2)
        return []

    def outbox_size(self) -> int:
        return len(self.outbox.read_text().splitlines()) if self.outbox.exists() else 0

    def send(self, body: str, *, sender: str = SENDER, sid: str | None = None, expect_reply: bool = True) -> str:
        sid = sid or "SM" + secrets.token_hex(16)
        params = self.form(body, sid, sender)
        offset = self.outbox_size()
        response = self.post(params, self.validator.compute_signature(self.signed_url, params))
        if response.status_code != 200:
            raise AssertionError(f"HTTP {response.status_code} for {body!r}")
        replies = self.replies_since(offset, wait=20.0 if expect_reply else 3.0)
        return replies[0] if replies else ""

    def check(self, name: str, ok: bool, detail: str = "") -> None:
        self.steps.append({"step": name, "ok": bool(ok), "detail": detail})
        self.failures += 0 if ok else 1
        print(f"{'PASS' if ok else 'FAIL'}  {name}{'  - ' + detail if detail and not ok else ''}")

    def gh(self, path: str) -> dict | list:
        response = self.github.get(path)
        response.raise_for_status()
        return response.json()

    def eventually(self, path: str, predicate, timeout: float = 45.0):
        """GitHub's list endpoints lag writes by seconds; poll until they agree (or time out)."""
        deadline = time.time() + timeout
        while True:
            value = self.gh(path)
            if predicate(value) or time.time() > deadline:
                return value
            time.sleep(2)


def run(h: Harness) -> None:
    nonce = secrets.token_hex(4)
    repo = h.repo

    reply = h.send("whoami")
    h.check("registered sender is recognised", reply.startswith("You are "), reply)

    reply = h.send(f"repo {repo}")
    h.check("repo overview reads the private sandbox", reply.splitlines()[0].lower().startswith(repo.lower()), reply)

    reply = h.send(f"new issue {repo} E2E {nonce} -- Created by the Git SMS signed-webhook e2e run.")
    code = re.search(r"CONFIRM (\d{4})", reply)
    h.check("write asks for a confirmation code", bool(code), reply)
    before = h.gh(f"/repos/{repo}/issues?state=all&per_page=100")
    h.check(
        "nothing is written before confirmation",
        not any(nonce in issue["title"] for issue in before),
        "issue already exists",
    )

    wrong = f"{(int(code.group(1)) + 1) % 10000:04d}" if code else "0000"
    reply = h.send(f"confirm {wrong}")
    h.check("a wrong code is refused", reply.startswith("Wrong code"), reply)

    confirm_sid = "SM" + secrets.token_hex(16)
    reply = h.send(f"confirm {code.group(1) if code else '0000'}", sid=confirm_sid)
    created = re.search(r"#(\d+): (https://\S+)", reply)
    h.check("confirmed write creates the issue", bool(created), reply)
    number = int(created.group(1)) if created else 0
    issue = h.gh(f"/repos/{repo}/issues/{number}") if number else {}
    h.check("GitHub has the issue with the exact title", issue.get("title") == f"E2E {nonce}", str(issue.get("title")))
    body = issue.get("body") or ""
    h.check("issue body carries alias attribution", "via Git SMS" in body, body[-80:])
    h.check("issue body never contains the phone number", SENDER not in body and SENDER[2:] not in body)

    reply = h.send(f"confirm {code.group(1) if code else '0000'}", sid=confirm_sid, expect_reply=False)
    listing = h.eventually(
        f"/repos/{repo}/issues?state=all&per_page=100", lambda v: any(nonce in i["title"] for i in v)
    )
    after = [i for i in listing if nonce in i["title"]]
    h.check("redelivered confirmation is a no-op", len(after) == 1 and reply == "", f"{len(after)} issues, {reply!r}")

    h.send(f"use {repo}")
    reply = h.send(f"comment #{number} Comment {nonce} from SMS")
    code = re.search(r"CONFIRM (\d{4})", reply)
    reply = h.send(f"confirm {code.group(1) if code else '0000'}")
    comments = h.gh(f"/repos/{repo}/issues/{number}/comments")
    h.check(
        "comment via default repo lands on GitHub",
        any(f"Comment {nonce} from SMS" in c["body"] for c in comments),
        reply,
    )

    reply = h.send(f"close #{number}")
    code = re.search(r"CONFIRM (\d{4})", reply)
    reply = h.send(f"confirm {code.group(1) if code else '0000'}")
    state = h.gh(f"/repos/{repo}/issues/{number}").get("state")
    h.check("close via SMS closes the issue", state == "closed", f"state={state}, reply={reply}")

    h.eventually(f"/repos/{repo}/issues?state=closed&per_page=100", lambda v: any(i["number"] == number for i in v))
    reply = h.send(f"issues {repo} closed")
    h.check("closed issue is listed", f"#{number} " in reply, reply)

    reply = h.send(f"new issue {repo.split('/')[0]}/not-granted-{nonce} Should be refused")
    h.check("write outside the grant is refused", reply.startswith("You can't change"), reply)

    reply = h.send("whoami", sender=OUTSIDER, expect_reply=False)
    h.check("unknown sender gets silence", reply == "", reply)

    params = h.form(f"new issue {repo} Forged {nonce}", "SM" + secrets.token_hex(16), SENDER)
    forged = h.post(params, "Zm9yZ2VkLXNpZ25hdHVyZQ==")
    unsigned = h.post(params, None)
    h.check("forged and unsigned webhooks are rejected", forged.status_code == unsigned.status_code == 403)
    time.sleep(2)
    forged_issues = [i for i in h.gh(f"/repos/{repo}/issues?state=all&per_page=100") if f"Forged {nonce}" in i["title"]]
    h.check("forged webhook wrote nothing", not forged_issues)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", required=True, help="where the server listens, e.g. http://127.0.0.1:8765")
    parser.add_argument("--public-url", help="GIT_SMS_PUBLIC_URL, if the server signs against another base")
    parser.add_argument("--repo", required=True, help="disposable owner/repo the sender may write to")
    parser.add_argument("--outbox", required=True)
    parser.add_argument("--evidence", required=True)
    args = parser.parse_args()

    harness = Harness(args)
    started = time.time()
    try:
        run(harness)
    except Exception as exc:  # report, don't hide
        harness.check("harness completed", False, f"{type(exc).__name__}: {exc}")
    Path(args.evidence).write_text(
        json.dumps(
            {
                "kind": "signed-webhook e2e (real GitHub, simulated Twilio)",
                "repo": args.repo,
                "started": started,
                "seconds": round(time.time() - started, 1),
                "passed": sum(step["ok"] for step in harness.steps),
                "failed": harness.failures,
                "steps": harness.steps,
            },
            indent=2,
        )
    )
    print(f"\n{sum(s['ok'] for s in harness.steps)} passed, {harness.failures} failed")
    return 1 if harness.failures else 0


if __name__ == "__main__":
    sys.exit(main())
