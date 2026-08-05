from commands import create_issue, create_repo, get_headers

def test_headers_do_not_emit_bearer_none(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    assert get_headers() == {"Accept": "application/vnd.github+json"}

def test_disabled_write_outcomes_are_explicit():
    assert create_repo("demo").message == "Repository creation is disabled in this proof of concept."
    assert create_issue("o/r", "title", "body").message == "Issue creation is disabled in this proof of concept."
