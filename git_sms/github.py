"""Minimal GitHub REST client: bounded timeouts, App or token auth, SMS-safe errors."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from datetime import datetime
from typing import Any

import httpx

from git_sms import __version__

API_VERSION = "2022-11-28"


class GitHubError(Exception):
    """A failure whose message is safe to text back to the sender.

    `ambiguous` is set when a write may have taken effect anyway (a timeout after the
    request was sent, a dropped connection, a 5xx): such a write must never be retried
    blindly.
    """

    def __init__(self, message: str, status: int | None = None, ambiguous: bool = False):
        super().__init__(message)
        self.status = status
        self.ambiguous = ambiguous


class TokenAuth:
    def __init__(self, token: str):
        self._token = token

    def authorization(self) -> str:
        return f"Bearer {self._token}"


class AppAuth:
    """GitHub App installation tokens, minted from a JWT and cached until near expiry."""

    def __init__(
        self,
        app_id: str,
        private_key: str,
        installation_id: str,
        api_url: str,
        transport: httpx.BaseTransport | None = None,
        clock: Callable[[], float] = time.time,
    ):
        self._app_id = app_id
        self._private_key = private_key
        self._installation_id = installation_id
        self._api_url = api_url
        self._transport = transport
        self._clock = clock
        self._lock = threading.Lock()
        self._token: str | None = None
        self._expires_at = 0.0

    def _jwt(self) -> str:
        import jwt

        now = int(self._clock())
        return jwt.encode({"iat": now - 60, "exp": now + 540, "iss": self._app_id}, self._private_key, "RS256")

    def authorization(self) -> str:
        with self._lock:
            if self._token and self._expires_at - 300 > self._clock():
                return f"Bearer {self._token}"
            try:
                with httpx.Client(transport=self._transport, timeout=10.0) as client:
                    response = client.post(
                        f"{self._api_url}/app/installations/{self._installation_id}/access_tokens",
                        headers={
                            "Authorization": f"Bearer {self._jwt()}",
                            "Accept": "application/vnd.github+json",
                            "X-GitHub-Api-Version": API_VERSION,
                            "User-Agent": f"git-sms/{__version__}",
                        },
                    )
            except httpx.HTTPError as exc:
                raise GitHubError("Could not authenticate with GitHub. Try again later.") from exc
            if response.status_code != 201:
                raise GitHubError("GitHub rejected this service's App credentials.", response.status_code)
            payload = response.json()
            self._token = payload["token"]
            self._expires_at = datetime.fromisoformat(payload["expires_at"].replace("Z", "+00:00")).timestamp()
            return f"Bearer {self._token}"


def _error(response: httpx.Response) -> GitHubError:
    status = response.status_code
    if status == 429 or (status == 403 and response.headers.get("x-ratelimit-remaining") == "0"):
        return GitHubError("GitHub rate limit reached. Try again later.", status)
    if status == 401:
        return GitHubError("GitHub rejected this service's credentials.", status)
    if status == 403:
        return GitHubError("GitHub denied access to that.", status)
    if status == 404:
        return GitHubError("Not found, or this service cannot see it.", status)
    if status == 410:
        return GitHubError("That is gone or disabled on GitHub.", status)
    if status == 422:
        detail = ""
        try:
            errors = response.json().get("errors") or []
            first = errors[0] if errors else {}
            if isinstance(first, dict):
                detail = str(first.get("message") or first.get("code") or "")
            else:
                detail = str(first)
        except (ValueError, AttributeError):
            pass
        return GitHubError(f"GitHub rejected the request{': ' + detail[:120] if detail else '.'}", status)
    if status >= 500:
        return GitHubError("GitHub is having trouble. Try again later.", status)
    return GitHubError(f"GitHub returned an unexpected error ({status}).", status)


class GitHub:
    def __init__(
        self,
        api_url: str = "https://api.github.com",
        auth: TokenAuth | AppAuth | None = None,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 10.0,
    ):
        self._auth = auth
        self._web = (
            "https://github.com"
            if api_url.rstrip("/") == "https://api.github.com"
            else (api_url.rstrip("/").removesuffix("/api/v3"))
        )
        self._client = httpx.Client(
            base_url=api_url,
            transport=transport,
            timeout=timeout,
            headers={
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": API_VERSION,
                "User-Agent": f"git-sms/{__version__}",
            },
        )

    def web_url(self, path: str) -> str:
        return f"{self._web}/{path.lstrip('/')}"

    @property
    def authenticated(self) -> bool:
        return self._auth is not None

    def close(self) -> None:
        self._client.close()

    def _request(self, method: str, path: str, *, raw: bool = False, **kwargs: Any) -> Any:
        headers = {"Authorization": self._auth.authorization()} if self._auth else {}
        if raw:
            headers["Accept"] = "application/vnd.github.raw+json"
        write = method != "GET"
        try:
            response = self._client.request(method, path, headers=headers, **kwargs)
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout) as exc:
            raise GitHubError("Could not reach GitHub. Try again later.") from exc
        except httpx.TimeoutException as exc:
            raise GitHubError("GitHub did not respond in time.", ambiguous=write) from exc
        except httpx.HTTPError as exc:
            raise GitHubError("The connection to GitHub failed.", ambiguous=write) from exc
        if not response.is_success:
            error = _error(response)
            error.ambiguous = write and response.status_code >= 500
            raise error
        if raw:
            return response.text
        try:
            return response.json()
        except ValueError as exc:
            raise GitHubError("GitHub returned an unreadable response.") from exc

    # Reads -------------------------------------------------------------------------

    def repo(self, repo: str) -> dict:
        return self._request("GET", f"/repos/{repo}")

    def readme(self, repo: str) -> str:
        return self._request("GET", f"/repos/{repo}/readme", raw=True)

    def issues(self, repo: str, state: str = "open", limit: int = 5, max_pages: int = 4) -> list[dict]:
        """Newest issues, skipping pull requests (which the issues API interleaves) across bounded pages."""
        found: list[dict] = []
        for page in range(1, max_pages + 1):
            items = self._request("GET", f"/repos/{repo}/issues", params={"state": state, "per_page": 50, "page": page})
            found += [item for item in items if "pull_request" not in item]
            if len(found) >= limit or len(items) < 50:
                break
        return found[:limit]

    def issue(self, repo: str, number: int) -> dict:
        return self._request("GET", f"/repos/{repo}/issues/{number}")

    def comments(self, repo: str, number: int, max_pages: int = 3, since: str | None = None) -> list[dict]:
        """Oldest-first comments, up to max_pages pages of 100."""
        found: list[dict] = []
        params: dict[str, Any] = {"per_page": 100}
        if since:
            params["since"] = since
        for page in range(1, max_pages + 1):
            items = self._request("GET", f"/repos/{repo}/issues/{number}/comments", params={**params, "page": page})
            found += items
            if len(items) < 100:
                break
        return found

    def recent_issues(self, repo: str, since: str, max_pages: int = 3) -> list[dict]:
        """Issues and PRs updated since an ISO time, for reconciling an unconfirmed write."""
        found: list[dict] = []
        for page in range(1, max_pages + 1):
            items = self._request(
                "GET",
                f"/repos/{repo}/issues",
                params={
                    "state": "all",
                    "since": since,
                    "sort": "created",
                    "direction": "desc",
                    "per_page": 100,
                    "page": page,
                },
            )
            found += items
            if len(items) < 100:
                break
        return found

    def pulls(self, repo: str, limit: int = 5) -> list[dict]:
        return self._request("GET", f"/repos/{repo}/pulls", params={"state": "open", "per_page": limit})

    def pull(self, repo: str, number: int) -> dict:
        return self._request("GET", f"/repos/{repo}/pulls/{number}")

    def search_repositories(self, query: str, per_page: int = 30) -> list[dict]:
        payload = self._request("GET", "/search/repositories", params={"q": query, "per_page": per_page})
        return payload.get("items", [])

    # Writes ------------------------------------------------------------------------

    def create_issue(self, repo: str, title: str, body: str) -> dict:
        return self._request("POST", f"/repos/{repo}/issues", json={"title": title, "body": body})

    def add_comment(self, repo: str, number: int, body: str) -> dict:
        return self._request("POST", f"/repos/{repo}/issues/{number}/comments", json={"body": body})

    def set_state(self, repo: str, number: int, state: str) -> dict:
        return self._request("PATCH", f"/repos/{repo}/issues/{number}", json={"state": state})
