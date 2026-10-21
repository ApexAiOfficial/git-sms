import re
from datetime import UTC, datetime

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from git_sms.github import AppAuth, GitHub, GitHubError, TokenAuth

from .conftest import Clock, FakeGitHubAPI


def client_for(handler, auth=None):
    return GitHub("https://api.github.test", auth, transport=httpx.MockTransport(handler))


def test_standard_headers_and_token():
    api = FakeGitHubAPI()
    api.add("GET", "/repos/acme/docs", {"full_name": "acme/docs"})
    api.client(TokenAuth("t0ken")).repo("acme/docs")
    headers = api.requests[0].headers
    assert headers["Authorization"] == "Bearer t0ken"
    assert headers["X-GitHub-Api-Version"] == "2022-11-28"
    assert headers["User-Agent"].startswith("git-sms/")


def test_anonymous_client_sends_no_authorization():
    api = FakeGitHubAPI()
    api.add("GET", "/repos/acme/docs", {})
    api.client(None).repo("acme/docs")
    assert "Authorization" not in api.requests[0].headers


def test_readme_is_requested_raw():
    api = FakeGitHubAPI()
    api.add("GET", "/repos/acme/docs/readme", "# Title")
    assert api.client().readme("acme/docs") == "# Title"
    assert api.requests[0].headers["Accept"] == "application/vnd.github.raw+json"


@pytest.mark.parametrize(
    ("status", "headers", "body", "message"),
    [
        (401, {}, {}, "rejected this service's credentials"),
        (403, {"x-ratelimit-remaining": "0"}, {}, "rate limit"),
        (429, {}, {}, "rate limit"),
        (403, {}, {}, "denied access"),
        (404, {}, {}, "Not found"),
        (410, {}, {}, "gone or disabled"),
        (422, {}, {"errors": [{"message": "title is too long"}]}, "rejected the request: title is too long"),
        (422, {}, {"errors": ["plain"]}, "rejected the request: plain"),
        (422, {}, {}, "rejected the request."),
        (502, {}, {}, "having trouble"),
        (418, {}, {}, "unexpected error (418)"),
    ],
)
def test_error_mapping(status, headers, body, message):
    client = client_for(lambda request: httpx.Response(status, json=body, headers=headers))
    with pytest.raises(GitHubError, match=re.escape(message)) as caught:
        client.repo("acme/docs")
    assert caught.value.status == status


def test_network_failures_are_safe_messages():
    def timeout(request):
        raise httpx.ReadTimeout("slow", request=request)

    def refused(request):
        raise httpx.ConnectError("refused", request=request)

    with pytest.raises(GitHubError, match="did not respond in time"):
        client_for(timeout).repo("acme/docs")
    with pytest.raises(GitHubError, match="Could not reach GitHub"):
        client_for(refused).repo("acme/docs")


def test_issue_listing_filters_pull_requests_and_limits():
    api = FakeGitHubAPI()
    items = [{"number": n, "pull_request": {}} if n % 2 else {"number": n} for n in range(20)]
    api.add("GET", "/repos/acme/docs/issues", items)
    assert [i["number"] for i in api.client().issues("acme/docs", limit=3)] == [0, 2, 4]


def test_write_payloads():
    api = FakeGitHubAPI()
    api.add("POST", "/repos/acme/docs/issues", {"number": 1}, 201)
    api.add("POST", "/repos/acme/docs/issues/1/comments", {}, 201)
    api.add("PATCH", "/repos/acme/docs/issues/1", {})
    client = api.client()
    client.create_issue("acme/docs", "T", "B")
    client.add_comment("acme/docs", 1, "C")
    client.set_state("acme/docs", 1, "closed")
    assert [r.content for r in api.requests] == [b'{"title":"T","body":"B"}', b'{"body":"C"}', b'{"state":"closed"}']


@pytest.fixture(scope="module")
def rsa_key():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()
    return key, pem


def test_app_auth_mints_and_caches_installation_tokens(rsa_key):
    key, pem = rsa_key
    clock = Clock()
    minted = []

    def handler(request):
        assert request.url.path == "/app/installations/42/access_tokens"
        claims = jwt.decode(
            request.headers["Authorization"].removeprefix("Bearer "),
            key.public_key(),
            algorithms=["RS256"],
            options={"verify_exp": False, "verify_iat": False},
        )
        assert claims["iss"] == "123"
        assert claims["exp"] - claims["iat"] <= 600
        minted.append(claims)
        expires = datetime.fromtimestamp(clock.now + 3600, UTC).isoformat().replace("+00:00", "Z")
        return httpx.Response(201, json={"token": f"ghs_{len(minted)}", "expires_at": expires})

    auth = AppAuth("123", pem, "42", "https://api.github.test", transport=httpx.MockTransport(handler), clock=clock)
    assert auth.authorization() == "Bearer ghs_1"
    clock.advance(3000)
    assert auth.authorization() == "Bearer ghs_1"
    clock.advance(301)
    assert auth.authorization() == "Bearer ghs_2"
    assert len(minted) == 2


def test_app_auth_failure_is_safe(rsa_key):
    _, pem = rsa_key
    auth = AppAuth(
        "123",
        pem,
        "42",
        "https://api.github.test",
        transport=httpx.MockTransport(lambda request: httpx.Response(401, json={"message": "Bad credentials"})),
    )
    with pytest.raises(GitHubError, match="App credentials"):
        auth.authorization()
