#!/usr/bin/env python3
"""Carrier-delivered SMS end-to-end runs of Git SMS, over two independent transports.

Both runs use the real Git SMS core (service, store, worker) in this process, a real
GitHub sandbox repository, and real SMS through carrier networks. Only the transport
differs, and every result is checked from an independent record.

  --path android   The handset is Git SMS's SMS gateway: messages it receives are read
                   through its git-sms-gate and replies leave from its own SIM. No Twilio
                   in Git SMS. A Twilio number plays the user: commands are sent to the
                   handset through an external SMS API (any harness able to send from that number), and the
                   handset's replies are read back from Twilio's message log.
  --path twilio    Git SMS uses a Twilio number: inbound messages are read from Twilio's
                   message log (that number's webhook belongs to another app, so polling
                   stands in for the webhook leg, which signed_webhook_e2e.py covers),
                   and replies go out through Git SMS's real TwilioSender. The handset
                   plays the user, sending through its gate and reading replies from its
                   own inbox; Twilio's delivery status is checked for each reply.

The flow is WHOAMI, REPO, NEW ISSUE, CONFIRM; the issue is then verified on GitHub. It is
bounded: four commands, a few minutes, and only messages the driver itself sent are
processed, so an automatic acknowledgement can never start a reply loop.

Environment: GITHUB_TOKEN; TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN; TWILIO_NUMBER and
HANDSET_NUMBER (E.164); HANDSET_SSH (ssh host alias) and HANDSET_KEY (the gate key);
for --path android also HARNESS_SMS_API_URL and HARNESS_SMS_API_KEY: an HTTP API that accepts
POST /api/v1/communications {"channel": "sms", "to": ..., "body": ...} and sends from the Twilio
number. Phone numbers are never printed.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import sys
import tempfile
import threading
import time
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(1, str(Path(__file__).resolve().parents[1]))  # the git_sms package
from handset import Handset, HandsetSender

from git_sms.app import TwilioSender, build_service
from git_sms.config import Settings
from git_sms.worker import Worker

STEP_TIMEOUT = 180


def mask(number: str) -> str:
    return number[:-4] + "XXXX"


class TwilioLog:
    """Read-only view of the Twilio account's message log."""

    def __init__(self, sid: str, token: str):
        self.base = f"https://api.twilio.com/2010-04-01/Accounts/{sid}"
        self.client = httpx.Client(auth=(sid, token), timeout=30)

    def messages(self, to: str, from_: str, after: datetime) -> list[dict]:
        params = {"To": to, "From": from_, "DateSent>": after.strftime("%Y-%m-%d"), "PageSize": 50}
        items = self.client.get(f"{self.base}/Messages.json", params=params).json().get("messages", [])
        fresh = [m for m in items if parsedate_to_datetime(m["date_created"]) >= after]
        return sorted(fresh, key=lambda m: parsedate_to_datetime(m["date_created"]))

    def message(self, message_sid: str) -> dict:
        return self.client.get(f"{self.base}/Messages/{message_sid}.json").json()


class ExternalSmsApi:
    """An external harness that sends SMS from the Twilio number (it is not part of Git SMS)."""

    def __init__(self, url: str, key: str):
        self.client = httpx.Client(base_url=url.rstrip("/"), timeout=30, headers={"Authorization": f"Bearer {key}"})

    def send(self, to: str, body: str) -> dict:
        response = self.client.post(
            "/api/v1/communications",
            json={"channel": "sms", "to": to, "body": body},
            headers={"Idempotency-Key": f"git-sms-e2e-{secrets.token_hex(8)}"},
        )
        payload = response.json() if response.content else {}
        if response.status_code >= 300:
            raise RuntimeError(f"the SMS API refused the send: HTTP {response.status_code} {str(payload)[:160]}")
        return payload


class Run:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.twilio_number, self.handset_number = os.environ["TWILIO_NUMBER"], os.environ["HANDSET_NUMBER"]
        self.handset = Handset(os.environ["HANDSET_SSH"], os.environ["HANDSET_KEY"])
        self.log = TwilioLog(os.environ["TWILIO_ACCOUNT_SID"], os.environ["TWILIO_AUTH_TOKEN"])
        self.github = httpx.Client(
            base_url="https://api.github.com",
            timeout=30,
            headers={"Authorization": f"Bearer {os.environ['GITHUB_TOKEN']}", "Accept": "application/vnd.github+json"},
        )
        self.steps: list[dict] = []
        self.sent_bodies: set[str] = set()
        self.ignored = 0
        self.stop = threading.Event()
        self.started = datetime.now(UTC)

    def check(self, name: str, ok: bool, detail: str = "", **extra) -> None:
        self.steps.append({"step": name, "ok": bool(ok), "detail": detail[:200], **extra})
        print(f"{'PASS' if ok else 'FAIL'}  {name}{'  - ' + detail[:160] if detail and not ok else ''}")

    def git_sms(self, user: str, alias: str, sender_backend: str):
        work = Path(tempfile.mkdtemp(prefix="git-sms-live-", dir=self.args.state_dir))
        users = work / "users.toml"
        repo = self.args.repo
        users.write_text(f'[[users]]\nphone = "{user}"\nalias = "{alias}"\nread = ["{repo}"]\nwrite = ["{repo}"]\n')
        users.chmod(0o600)
        twilio = sender_backend == "twilio"
        settings = Settings(
            environment="development",
            sender_backend=sender_backend,
            allow_unsigned=not twilio,
            db_path=str(work / "git-sms.db"),
            users_file=str(users),
            github_token=os.environ["GITHUB_TOKEN"],
            twilio_account_sid=os.environ["TWILIO_ACCOUNT_SID"] if twilio else None,
            twilio_auth_token=os.environ["TWILIO_AUTH_TOKEN"] if twilio else None,
            workers=2,
        )
        settings.validate()
        return settings, build_service(settings)

    def enqueue(self, service, worker, message_sid: str, sender: str, recipient: str, body: str) -> None:
        if body not in self.sent_bodies:
            self.ignored += 1  # not one of ours (for example an automatic acknowledgement): never processed
            return
        key = service.store.sender_key(sender)
        if service.store.enqueue_inbound(message_sid, key, {"from": sender, "to": recipient, "body": body}) == "queued":
            worker.wake()

    def wait_for(self, fetch, predicate, label: str) -> tuple[dict | None, float]:
        t0 = time.time()
        while time.time() - t0 < STEP_TIMEOUT:
            for item in fetch():
                if predicate(item):
                    return item, round(time.time() - t0, 1)
            time.sleep(4)
        return None, round(time.time() - t0, 1)

    # The four-command conversation, shared by both paths --------------------------------

    def converse(self, send, replies, sender_alias: str) -> int | None:
        nonce = secrets.token_hex(4)
        title = f"Live SMS {self.args.path} {nonce}"
        steps = [
            ("whoami", lambda b: b.startswith(f"You are {sender_alias}.")),
            (f"repo {self.args.repo}", lambda b: b.lower().startswith(self.args.repo.lower())),
            (f"new issue {self.args.repo} {title} -- sent over carrier SMS", lambda b: "Reply CONFIRM" in b),
        ]
        seen: set[str] = set()
        code = None
        for body, expected in steps:
            self.sent_bodies.add(body)
            t0 = time.time()
            send(body)
            reply, waited = self.wait_for(
                replies, lambda r, expected=expected: r["key"] not in seen and expected(r["body"]), body
            )
            label = " ".join(body.split()[:2] if body.startswith("new") else body.split()[:1]).upper()
            self.check(
                f"reply to {label}",
                reply is not None,
                f"no matching reply in {waited}s",
                seconds=round(time.time() - t0, 1),
            )
            if reply is None:
                return None
            seen.add(reply["key"])
            match = re.search(r"CONFIRM (\d{4})", reply["body"])
            code = match.group(1) if match else code
        confirm = f"confirm {code}"
        self.sent_bodies.add(confirm)
        t0 = time.time()
        send(confirm)
        reply, waited = self.wait_for(
            replies, lambda r: r["key"] not in seen and r["body"].startswith("Created "), confirm
        )
        self.check(
            "reply to CONFIRM", reply is not None, f"no matching reply in {waited}s", seconds=round(time.time() - t0, 1)
        )
        if reply is None:
            return None
        number = int(re.search(r"#(\d+):", reply["body"]).group(1))
        issue = self.github.get(f"/repos/{self.args.repo}/issues/{number}").json()
        body = issue.get("body") or ""
        self.check(
            "GitHub has the issue from the SMS conversation", issue.get("title") == title, str(issue.get("title"))
        )
        self.check(
            "the issue carries alias attribution and its marker", "via Git SMS" in body and "<!-- git-sms:" in body
        )
        self.check(
            "no phone number reached GitHub",
            all(n not in body and n[2:] not in body for n in (self.twilio_number, self.handset_number)),
        )
        return number

    # Path 1: the handset is the gateway -----------------------------------------------------

    def android(self) -> None:
        ping = self.handset.ping()
        self.check("handset gate reachable with SMS access", ping.get("sms_read") == "ok", json.dumps(ping))
        if ping.get("sms_read") != "ok":
            return
        settings, service = self.git_sms(self.twilio_number, "twilio-harness", "console")
        worker = Worker(service, HandsetSender(self.handset), settings)
        known = {m["key"] for m in self.handset.inbox(self.twilio_number, 50)}

        def gateway() -> None:
            while not self.stop.is_set():
                try:
                    for m in reversed(self.handset.inbox(self.twilio_number, 20)):
                        if m["key"] not in known:
                            known.add(m["key"])
                            self.enqueue(
                                service, worker, "AN" + m["key"][:32], self.twilio_number, "handset", m["body"]
                            )
                except Exception as error:  # keep polling; report in the summary
                    print(f"  gateway poll: {type(error).__name__}")
                self.stop.wait(3)

        comms = ExternalSmsApi(os.environ["HARNESS_SMS_API_URL"], os.environ["HARNESS_SMS_API_KEY"])

        def send(body: str) -> None:
            result = comms.send(self.handset_number, body)
            if result.get("gate_reason_code") not in (None, "PERMITTED"):
                raise RuntimeError(f"the SMS API declined: {result.get('gate_reason_code')}")

        def replies() -> list[dict]:
            items = self.log.messages(self.twilio_number, self.handset_number, self.started)
            return [{"key": m["sid"], "body": m["body"]} for m in items]

        worker.start()
        poller = threading.Thread(target=gateway, daemon=True)
        poller.start()
        try:
            self.converse(send, replies, "twilio-harness")
        finally:
            self.stop.set()
            poller.join(10)
            worker.stop()

    # Path 2: Git SMS on Twilio ----------------------------------------------------------------

    def twilio(self) -> None:
        ping = self.handset.ping()
        self.check("handset gate reachable with SMS access", ping.get("sms_read") == "ok", json.dumps(ping))
        if ping.get("sms_read") != "ok":
            return
        settings, service = self.git_sms(self.handset_number, "handset", "twilio")
        worker = Worker(service, TwilioSender(settings), settings)
        known = {m["key"] for m in self.handset.inbox(self.twilio_number, 50)}
        ingested: set[str] = set()
        inbound_sids: list[str] = []

        def ingest() -> None:
            while not self.stop.is_set():
                try:
                    for m in self.log.messages(self.twilio_number, self.handset_number, self.started):
                        if m["sid"] not in ingested:
                            ingested.add(m["sid"])
                            inbound_sids.append(m["sid"])
                            self.enqueue(service, worker, m["sid"], self.handset_number, self.twilio_number, m["body"])
                except Exception as error:
                    print(f"  twilio poll: {type(error).__name__}")
                self.stop.wait(3)

        def send(body: str) -> None:
            self.handset.send(self.twilio_number, body)

        def replies() -> list[dict]:
            return [m for m in self.handset.inbox(self.twilio_number, 20) if m["key"] not in known]

        worker.start()
        poller = threading.Thread(target=ingest, daemon=True)
        poller.start()
        try:
            self.converse(send, replies, "handset")
        finally:
            self.stop.set()
            poller.join(10)
            worker.stop()
        provider_sids = [row[1] for sid in inbound_sids for row in service.store.outbound_for(sid) if row[1]]
        statuses = [self.log.message(p).get("status") for p in provider_sids]
        self.check(
            "Twilio reports every Git SMS reply delivered",
            bool(statuses) and all(s == "delivered" for s in statuses),
            f"statuses: {statuses}",
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--path", choices=["android", "twilio"], required=True)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--state-dir", required=True, help="private directory for the run's database and users file")
    parser.add_argument("--evidence", required=True)
    args = parser.parse_args()
    run = Run(args)
    t0 = time.time()
    try:
        getattr(run, args.path)()
    except Exception as error:
        run.check("run completed", False, f"{type(error).__name__}: {error}")
    Path(args.evidence).write_text(
        json.dumps(
            {
                "kind": f"carrier SMS e2e, {args.path} transport",
                "repo": args.repo,
                "twilio_number": mask(run.twilio_number),
                "handset_number": mask(run.handset_number),
                "started": run.started.isoformat(),
                "seconds": round(time.time() - t0, 1),
                "ignored_foreign_messages": run.ignored,
                "passed": sum(s["ok"] for s in run.steps),
                "failed": sum(not s["ok"] for s in run.steps),
                "steps": run.steps,
            },
            indent=2,
        )
    )
    failed = sum(not s["ok"] for s in run.steps)
    print(f"\n{len(run.steps) - failed} passed, {failed} failed ({round(time.time() - t0)}s)")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
