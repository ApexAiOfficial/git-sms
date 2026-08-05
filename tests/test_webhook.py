from fastapi.testclient import TestClient

import main

client = TestClient(main.app)


def post(body: str):
    return client.post(
        "/webhook",
        data={"From": "+15555550123", "Body": body},
    )


def test_help_returns_valid_twiml():
    response = post("help")
    assert response.status_code == 200
    assert "Available commands" in response.text


def test_deterministic_write_commands_are_disabled():
    assert "Repository creation is disabled" in post("create repo demo").text
    assert (
        "Issue creation is disabled"
        in post("create issue owner/repo title -- body").text
    )


def test_missing_llm_credentials_returns_useful_twiml(monkeypatch):
    monkeypatch.delenv("GITHUB_OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    response = post("something unrecognized")
    assert response.status_code == 200
    assert "AI command parsing is not configured" in response.text


def test_unexpected_exception_returns_safe_twiml(monkeypatch):
    def raise_runtime_error(**kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(main, "handle_message", raise_runtime_error)
    response = post("help")
    assert response.status_code == 200
    assert main.SAFE_ERROR in response.text
