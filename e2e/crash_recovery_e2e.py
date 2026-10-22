#!/usr/bin/env python3
"""Kill a real Git SMS server with work still queued, restart it, and prove nothing is lost or repeated.

It starts `python -m git_sms` itself (configured from this process's environment, as
for signed_webhook_e2e.py), queues a burst of reads followed by a confirmed write from
one sender, SIGKILLs the server immediately after the last webhook is acknowledged,
restarts it on the same database, and then checks through the GitHub API that the
write landed exactly once and that every acknowledged message was answered.

  e2e/crash_recovery_e2e.py --url http://127.0.0.1:8765 --repo owner/sandbox \\
      --outbox /path/outbox.jsonl --evidence /path/crash-evidence.json
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import signal
import subprocess
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
from signed_webhook_e2e import Harness

READS = 8


def start(url: str) -> subprocess.Popen:
    process = subprocess.Popen([sys.executable, "-m", "git_sms"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(60):
        try:
            if httpx.get(url + "/healthz", timeout=1).status_code == 200:
                return process
        except httpx.HTTPError:
            pass
        time.sleep(0.25)
    process.kill()
    raise RuntimeError("server did not become healthy")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", required=True)
    parser.add_argument("--public-url")
    parser.add_argument("--repo", required=True)
    parser.add_argument("--outbox", required=True)
    parser.add_argument("--evidence", required=True)
    args = parser.parse_args()
    h = Harness(args)
    nonce = secrets.token_hex(4)
    started = time.time()

    server = start(args.url)
    try:
        h.send(f"use {args.repo}")
        reply = h.send(f"new issue Crash {nonce} -- Written once across a SIGKILL and restart.")
        code = re.search(r"CONFIRM (\d{4})", reply)
        h.check("confirmation code issued before the crash", bool(code), reply)

        offset = h.outbox_size()
        acknowledged = 0
        for _ in range(READS):
            sid = "SM" + secrets.token_hex(16)
            params = h.form(f"repo {args.repo}", sid, "+15550100001")
            acknowledged += h.post(params, h.validator.compute_signature(h.signed_url, params)).status_code == 200
        sid = "SM" + secrets.token_hex(16)
        params = h.form(f"confirm {code.group(1) if code else '0000'}", sid, "+15550100001")
        acknowledged += h.post(params, h.validator.compute_signature(h.signed_url, params)).status_code == 200
        server.send_signal(signal.SIGKILL)
        server.wait()
        answered_before = h.outbox_size() - offset
        h.check("every message in the burst was acknowledged", acknowledged == READS + 1, str(acknowledged))
        h.check(
            "the crash left work unfinished (the test is meaningful)",
            answered_before < READS + 1,
            f"{answered_before} of {READS + 1} answered before the kill",
        )
    finally:
        if server.poll() is None:
            server.kill()

    server = start(args.url)
    try:
        replies = h.replies_since(offset, wait=5)
        deadline = time.time() + 60
        while len(replies) < READS + 1 and time.time() < deadline:
            time.sleep(1)
            replies = h.replies_since(offset, wait=1)
        h.check("every acknowledged message was answered after restart", len(replies) >= READS + 1, str(len(replies)))
        created = [r for r in replies if r.startswith(("Created ", "Already done: ", "Done: "))]
        h.check("the confirmation was answered exactly once", len(created) == 1, json.dumps(created))
        listing = h.eventually(
            f"/repos/{args.repo}/issues?state=all&per_page=100", lambda v: any(nonce in i["title"] for i in v)
        )
        matches = [i for i in listing if nonce in i["title"]]
        h.check("GitHub has exactly one issue for the write", len(matches) == 1, str(len(matches)))
        marker = re.search(r"<!-- git-sms:([0-9a-f]+) -->", (matches[0].get("body") or "") if matches else "")
        h.check("the issue carries the reconciliation marker", bool(marker))
    finally:
        server.terminate()
        server.wait(timeout=15)

    Path(args.evidence).write_text(
        json.dumps(
            {
                "kind": "SIGKILL crash-recovery e2e (real GitHub, simulated Twilio)",
                "repo": args.repo,
                "started": started,
                "seconds": round(time.time() - started, 1),
                "passed": sum(step["ok"] for step in h.steps),
                "failed": h.failures,
                "steps": h.steps,
            },
            indent=2,
        )
    )
    print(f"\n{sum(s['ok'] for s in h.steps)} passed, {h.failures} failed")
    return 1 if h.failures else 0


if __name__ == "__main__":
    os.environ.setdefault("PYTHONUNBUFFERED", "1")
    sys.exit(main())
