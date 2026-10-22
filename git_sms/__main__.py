"""Run the server, or inspect and settle its state.

    python -m git_sms                       serve (configuration comes from the environment)
    python -m git_sms status                queue, reply and write health, and every unsettled write
    python -m git_sms reconcile             look for unsettled writes on GitHub and settle those found
    python -m git_sms resolve OP done URL   record an operator's verdict on one write after checking GitHub
                                            (unknown, observed, or an approved write that was never attempted)
    python -m git_sms resolve OP failed

`status` and `resolve` only read and write the database; `reconcile` also reads GitHub.
None of them prints a phone number or message text.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import UTC, datetime

import uvicorn


def _service():
    from git_sms.app import build_service
    from git_sms.config import Settings

    settings = Settings.from_env()
    settings.validate()
    return build_service(settings)


def _when(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, UTC).strftime("%Y-%m-%d %H:%M UTC")


def status() -> int:
    service = _service()
    print(json.dumps(service.store.health(), indent=2))
    unsettled = service.store.unresolved_operations()
    for op in unsettled:
        target = f"{op.repo}#{op.number}" if op.number else op.repo
        print(f"{op.op_id}  {op.status:<9}  {op.action:<16}  {target}  since {_when(op.created_at)}")
    return 1 if any(op.status == "unknown" for op in unsettled) else 0


def reconcile() -> int:
    service = _service()
    remaining = 0
    for op in service.store.unresolved_operations():
        if op.status != "unknown":
            continue  # confirmed and executing writes belong to the running server
        found = service.reconcile(op)
        if found and found[1]:
            print(f"{op.op_id}  done (proven by marker)  {found[0]}")
        elif found:
            print(f"{op.op_id}  observed (target state only; resolve it once you have checked)  {found[0]}")
        else:
            remaining += 1
            print(f"{op.op_id}  still unknown: check {service.github.web_url(op.repo + '/issues')} and run resolve")
    return 1 if remaining else 0


def resolve(op_id: str, verdict: str, url: str | None) -> int:
    service = _service()
    op = service.store.operation(op_id)
    if op is None or op.status not in {"unknown", "executing", "observed", "confirmed"}:
        print(f"{op_id}: no unsettled write with that id", file=sys.stderr)
        return 2
    service.store.finish_operation(op_id, verdict, url)
    service.store.audit(sender_key="operator", action=op.action, repo=op.repo, outcome=f"resolved_{verdict}", url=url)
    print(f"{op_id}: {verdict}")
    return 0


def serve() -> None:
    uvicorn.run(
        "git_sms.app:create_app",
        factory=True,
        host=os.getenv("GIT_SMS_HOST", "127.0.0.1"),
        port=int(os.getenv("PORT", "8000")),
        proxy_headers=False,
        access_log=False,
    )


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(), format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    parser = argparse.ArgumentParser(
        prog="git-sms", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("serve")
    commands.add_parser("status")
    commands.add_parser("reconcile")
    settle = commands.add_parser("resolve")
    settle.add_argument("op_id")
    settle.add_argument("verdict", choices=["done", "failed"])
    settle.add_argument("url", nargs="?")
    args = parser.parse_args(argv)

    if args.command in (None, "serve"):
        serve()
        return 0
    if args.command == "status":
        return status()
    if args.command == "reconcile":
        return reconcile()
    if args.verdict == "done" and not args.url:
        parser.error("resolve ... done needs the URL of the change on GitHub")
    return resolve(args.op_id, args.verdict, args.url)


def cli() -> None:
    sys.exit(main())


if __name__ == "__main__":
    cli()
