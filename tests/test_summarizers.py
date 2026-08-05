import summarizers


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.ok = 200 <= status_code < 300

    def json(self):
        return self._payload


def test_latest_issue_skips_pull_requests(monkeypatch):
    calls = []

    def fake_get(url, **kwargs):
        calls.append(url)
        if url.endswith("/issues"):
            return FakeResponse(
                [
                    {"number": 9, "pull_request": {}},
                    {"number": 8, "title": "Real issue"},
                ]
            )
        if url.endswith("/issues/8"):
            return FakeResponse(
                {"number": 8, "title": "Real issue", "body": "body"}
            )
        if url.endswith("/issues/8/comments"):
            return FakeResponse([])
        raise AssertionError(url)

    monkeypatch.setattr(summarizers.requests, "get", fake_get)
    monkeypatch.setattr(summarizers, "complete_text", lambda **_: "summary")
    assert summarizers.summarize_latest_issue("owner/repo") == "summary"
    assert any(url.endswith("/issues/8") for url in calls)


def test_repo_summary_without_llm_credentials_is_safe(monkeypatch):
    def fake_get(url, **kwargs):
        if url.endswith("/readme"):
            return FakeResponse({"content": ""})
        return FakeResponse({"name": "repo", "owner": {"login": "owner"}})

    def raise_not_configured(**kwargs):
        raise summarizers.LLMNotConfiguredError

    monkeypatch.setattr(summarizers.requests, "get", fake_get)
    monkeypatch.setattr(summarizers, "num_tokens", lambda _: 1)
    monkeypatch.setattr(summarizers, "complete_text", raise_not_configured)
    assert (
        summarizers.summarize_any_repo("owner/repo")
        == summarizers.SUMMARY_NOT_CONFIGURED
    )
