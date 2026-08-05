"""FastAPI webhook edge for Git SMS."""

from __future__ import annotations

import html
import logging
from typing import Annotated

from dotenv import load_dotenv
from fastapi import FastAPI, Form
from fastapi.responses import Response

from commands import create_issue, create_repo
from natural_language_router import route_natural_command
from summarizers import summarize_any_repo, summarize_latest_issue, summarize_specific_issue

load_dotenv()
log = logging.getLogger(__name__)
app = FastAPI(title="Git SMS", version="0.1.0")

HELP_TEXT = (
    "Available commands:\n"
    "- summarize owner/repo\n"
    "- summarize owner/repo issue\n"
    "- summarize owner/repo issue 123\n"
    "Write commands remain disabled in this proof of concept."
)
SAFE_ERROR = "Something went wrong while processing that message. Please try again later."


def handle_message(*, sender: str, body: str) -> str:  # noqa: ARG001
    """Handle one normalized inbound message and return reply text."""
    text = body.strip()
    parts = text.split()
    lower = text.lower()

    if lower == "help":
        return HELP_TEXT
    if lower.startswith("create repo") and len(parts) >= 3:
        return create_repo(parts[2]).message
    if lower.startswith("create issue") and len(parts) >= 4:
        try:
            pre, issue_body = text.split(" -- ", 1)
            _, _, repo, *title_parts = pre.split()
        except ValueError:
            return "Usage: create issue <owner/repo> <title> -- <body>"
        return create_issue(repo, " ".join(title_parts), issue_body).message
    if lower.startswith("summarize") and len(parts) >= 2 and "/" in parts[1]:
        repo = parts[1]
        if len(parts) >= 4 and parts[2].lower() == "issue" and parts[3].isdigit():
            return summarize_specific_issue(repo, int(parts[3]))
        if len(parts) >= 3 and parts[2].lower() == "issue":
            return summarize_latest_issue(repo)
        return summarize_any_repo(repo)

    fallback = route_natural_command(text)
    return fallback or "Unrecognized command. Text HELP for available commands."


def twilio_reply(message: str) -> Response:
    """Return a valid TwiML message response."""
    escaped = html.escape(message or "No content.")
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        "<Response>\n"
        f"    <Message>{escaped}</Message>\n"
        "</Response>"
    )
    return Response(content=xml, media_type="application/xml", status_code=200)


@app.post("/webhook")
async def sms_webhook(
    From: Annotated[str, Form()],  # noqa: N803
    Body: Annotated[str, Form()],  # noqa: N803
) -> Response:
    """Translate Twilio form fields at the edge and always return useful TwiML."""
    try:
        message = handle_message(sender=From, body=Body)
    except Exception:
        log.exception("Unexpected dispatch failure")
        message = SAFE_ERROR
    return twilio_reply(message)
