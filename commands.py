"""GitHub command utilities and intentionally disabled write operations."""

from __future__ import annotations

import os
from dataclasses import dataclass

import requests

GITHUB_API = "https://api.github.com"
REQUEST_TIMEOUT = 10


@dataclass(frozen=True)
class CommandOutcome:
    enabled: bool
    message: str


def get_headers() -> dict[str, str]:
    headers = {"Accept": "application/vnd.github+json"}
    token = os.getenv("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def get_authenticated_username() -> str | None:
    if not os.getenv("GITHUB_TOKEN"):
        return None
    try:
        response = requests.get(f"{GITHUB_API}/user", headers=get_headers(), timeout=REQUEST_TIMEOUT)
        if not response.ok:
            return None
        payload = response.json()
    except (requests.RequestException, ValueError):
        return None
    login = payload.get("login") if isinstance(payload, dict) else None
    return str(login) if login else None


def create_repo(name: str) -> CommandOutcome:  # noqa: ARG001
    return CommandOutcome(False, "Repository creation is disabled in this proof of concept.")


def create_issue(repo: str, title: str, body: str) -> CommandOutcome:  # noqa: ARG001
    return CommandOutcome(False, "Issue creation is disabled in this proof of concept.")
