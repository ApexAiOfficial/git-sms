"""Shared fixtures: a scriptable fake GitHub API, a fake LLM and a controllable clock.

Phone numbers here are fictional (+1 555-01xx range).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx
import pytest

from git_sms.config import Settings
from git_sms.github import GitHub, TokenAuth
from git_sms.identity import Directory
from git_sms.service import Inbound, Service
from git_sms.store import Store

ALICE = "+15550100001"  # reads everything, writes acme/field-*
BOB = "+15550100002"  # reads acme/* only
STRANGER = "+15550100009"  # not in the directory

USERS_TOML = f"""
[[users]]
phone = "{ALICE}"
alias = "alice"
read = ["*"]
write = ["acme/field-*"]

[[users]]
phone = "{BOB}"
alias = "bob"
read = ["acme/*"]
"""

REPO = {
    "full_name": "acme/field-reports",
    "description": "Reports from the field",
    "stargazers_count": 7,
    "forks_count": 2,
    "open_issues_count": 3,
    "language": "Python",
    "default_branch": "main",
    "html_url": "https://github.com/acme/field-reports",
    "private": False,
}
ISSUE_12 = {
    "number": 12,
    "title": "Pump 3 leaks",
    "state": "open",
    "user": {"login": "maria"},
    "comments": 2,
    "labels": [{"name": "bug"}],
    "body": "Water on the floor near pump 3.",
    "html_url": "https://github.com/acme/field-reports/issues/12",
}
PR_13 = {
    "number": 13,
    "title": "Fix pump driver",
    "state": "open",
    "user": {"login": "dev"},
    "pull_request": {"url": "x"},
    "html_url": "https://github.com/acme/field-reports/pull/13",
}


class FakeGitHubAPI:
    def __init__(self) -> None:
        self.routes: dict[tuple[str, str], tuple[int, Any, dict]] = {}
        self.requests: list[httpx.Request] = []

    def add(self, method: str, path: str, payload: Any, status: int = 200, headers: dict | None = None) -> None:
        self.routes[(method, path)] = (status, payload, headers or {})

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        route = self.routes.get((request.method, request.url.path))
        if route is None:
            return httpx.Response(404, json={"message": "Not Found"})
        status, payload, headers = route
        if callable(payload):
            payload = payload(request)
        if isinstance(payload, list) and "per_page" in request.url.params:
            # Paginate like GitHub, so code that reads only the first page is caught.
            per_page, page = int(request.url.params["per_page"]), int(request.url.params.get("page", 1))
            payload = payload[(page - 1) * per_page : page * per_page]
        if isinstance(payload, str):
            return httpx.Response(status, text=payload, headers=headers)
        return httpx.Response(status, json=payload, headers=headers)

    def client(self, auth: TokenAuth | None = TokenAuth("test-token")) -> GitHub:
        return GitHub("https://api.github.test", auth, transport=httpx.MockTransport(self.handler))

    def calls(self, method: str | None = None) -> list[str]:
        return [f"{r.method} {r.url.path}" for r in self.requests if method is None or r.method == method]


def standard_api() -> FakeGitHubAPI:
    api = FakeGitHubAPI()
    base = "/repos/acme/field-reports"
    api.add("GET", base, REPO)
    api.add("GET", "/repos/acme/docs", {**REPO, "full_name": "acme/docs", "html_url": "https://github.com/acme/docs"})
    api.add("GET", "/repos/other/secret", {**REPO, "full_name": "other/secret", "private": True})
    api.add("GET", f"{base}/issues", [ISSUE_12, PR_13, {**ISSUE_12, "number": 11, "title": "Gate", "comments": 0}])
    api.add("GET", f"{base}/issues/12", ISSUE_12)
    api.add("GET", f"{base}/issues/13", PR_13)
    api.add("GET", f"{base}/issues/12/comments", [{"user": {"login": "lee"}, "body": "Seen it too."}])
    api.add("GET", f"{base}/pulls", [{**PR_13, "draft": True}])
    api.add(
        "GET",
        f"{base}/pulls/13",
        {
            **PR_13,
            "merged": False,
            "mergeable": True,
            "draft": False,
            "head": {"ref": "fix-pump"},
            "base": {"ref": "main"},
            "additions": 12,
            "deletions": 3,
            "changed_files": 2,
        },
    )
    api.add("GET", f"{base}/readme", "# Field reports\nCollects reports.")
    api.add("GET", "/search/repositories", {"items": [{"full_name": "acme/field-reports", "description": "Reports"}]})
    api.add(
        "POST",
        f"{base}/issues",
        lambda request: {"number": 99, "html_url": "https://github.com/acme/field-reports/issues/99"},
        status=201,
    )
    api.add(
        "POST",
        f"{base}/issues/12/comments",
        {"html_url": "https://github.com/acme/field-reports/issues/12#issuecomment-1"},
        status=201,
    )
    api.add("PATCH", f"{base}/issues/12", {**ISSUE_12, "state": "closed"})
    return api


class FakeLLM:
    def __init__(self, interpret: str | None = None, summary: str = "A short summary.") -> None:
        self._interpret = interpret
        self._summary = summary
        self.interpreted: list[str] = []
        self.summarized: list[tuple[str, str]] = []

    def interpret(self, text: str) -> str | None:
        self.interpreted.append(text)
        return self._interpret

    def summarize(self, kind: str, content: str) -> str:
        self.summarized.append((kind, content))
        return self._summary


class Clock:
    def __init__(self) -> None:
        self.now = 1_800_000_000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def api() -> FakeGitHubAPI:
    return standard_api()


@pytest.fixture
def make_service(api: FakeGitHubAPI, clock: Clock) -> Callable[..., Service]:
    def make(
        llm: FakeLLM | None = None,
        public_read: bool = False,
        code: str = "4821",
        db_path: str = ":memory:",
        **overrides,
    ) -> Service:
        settings = Settings(environment="development", sender_backend="console", db_path=db_path, **overrides)
        directory = Directory.from_toml(("public_read = true\n" if public_read else "") + USERS_TOML)
        return Service(
            settings,
            Store(db_path, "k" * 32, clock=clock),
            directory,
            api.client(),
            guest_github=api.client(auth=None),
            llm=llm,
            code_source=lambda: code,
        )

    return make


class Conversation:
    """Send messages as one phone and read back the single reply."""

    def __init__(self, service: Service, phone: str) -> None:
        self.service = service
        self.phone = phone
        self.count = 0

    def send(self, body: str) -> str:
        self.count += 1
        sid = f"SM{self.phone[-4:]}{self.count:028d}"
        replies = self.service.handle(Inbound(sid, self.phone, "+15550100100", body))
        assert len(replies) <= 1
        return replies[0] if replies else ""
