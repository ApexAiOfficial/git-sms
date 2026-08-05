"""Optional natural-language routing for deterministic Git SMS commands."""

from __future__ import annotations

import json
import logging
from typing import Any

import requests

from commands import REQUEST_TIMEOUT, create_issue, create_repo, get_headers
from llm_client import LLMNotConfiguredError, complete_text
from summarizers import summarize_any_repo, summarize_latest_issue, summarize_specific_issue

log = logging.getLogger(__name__)
SUPPORTED_ACTIONS = {
    "summarize_repo",
    "summarize_latest_issue",
    "summarize_specific_issue",
    "create_repo",
    "create_issue",
    "unknown",
}
FALLBACK_NOT_CONFIGURED = (
    "I did not recognize that command, and AI command parsing is not configured. "
    "Text HELP for deterministic commands."
)


def github_search_repo(query: str) -> str | None:
    try:
        response = requests.get(
            "https://api.github.com/search/repositories",
            headers=get_headers(),
            params={
                "q": f"{query} in:name,description",
                "sort": "stars",
                "per_page": 10,
            },
            timeout=REQUEST_TIMEOUT,
        )
        payload = response.json() if response.ok else {}
    except (requests.RequestException, ValueError):
        return None

    items = payload.get("items", []) if isinstance(payload, dict) else []
    return str(items[0].get("full_name")) if items and isinstance(items[0], dict) else None


def parse_command_naturally(user_input: str) -> dict[str, Any]:
    prompt = (
        "Return JSON only with keys action, repo, repo_name, title, body, issue_number. "
        "Allowed actions: summarize_repo, summarize_latest_issue, summarize_specific_issue, "
        "create_repo, create_issue, unknown. Use null for missing values.\n\n"
        f"Input: {user_input}"
    )
    raw = complete_text(
        system_prompt="Extract a candidate GitHub command. Never execute it.",
        user_prompt=prompt,
        temperature=0.0,
        max_tokens=300,
    )
    if raw.startswith("```"):
        raw = raw.strip("`").strip()
        if raw.startswith("json"):
            raw = raw[4:].strip()

    parsed = json.loads(raw)
    if not isinstance(parsed, dict) or parsed.get("action") not in SUPPORTED_ACTIONS:
        return {"action": "unknown"}

    summary_actions = {
        "summarize_repo",
        "summarize_latest_issue",
        "summarize_specific_issue",
    }
    if parsed.get("action") in summary_actions and not parsed.get("repo"):
        parsed["repo"] = github_search_repo(user_input)
    return parsed


def route_natural_command(user_text: str) -> str | None:
    try:
        intent = parse_command_naturally(user_text)
    except LLMNotConfiguredError:
        return FALLBACK_NOT_CONFIGURED
    except (json.JSONDecodeError, TypeError, ValueError):
        return "I could not parse that request. Text HELP for available commands."
    except Exception:
        log.exception("Natural-language parsing failed")
        return "AI command parsing failed. Text HELP for deterministic commands."

    action, repo = intent.get("action"), intent.get("repo")
    if action == "summarize_repo" and isinstance(repo, str):
        return summarize_any_repo(repo)
    if action == "summarize_latest_issue" and isinstance(repo, str):
        return summarize_latest_issue(repo)
    if (
        action == "summarize_specific_issue"
        and isinstance(repo, str)
        and isinstance(intent.get("issue_number"), int)
    ):
        return summarize_specific_issue(repo, intent["issue_number"])
    if action == "create_repo" and isinstance(intent.get("repo_name"), str):
        return create_repo(intent["repo_name"]).message
    if action == "create_issue" and isinstance(repo, str):
        title = str(intent.get("title") or "Issue")
        body = str(intent.get("body") or "")
        return create_issue(repo, title, body).message
    return None
