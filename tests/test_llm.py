import httpx
import pytest

from git_sms.commands import CreateIssue, parse
from git_sms.llm import LLM, LLMError, normalize_interpretation


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("NEW ISSUE acme/x Title", "NEW ISSUE acme/x Title"),
        ("```\nNEW ISSUE acme/x Title\n```", "NEW ISSUE acme/x Title"),
        ("```text\nREPO acme/x\n```", "REPO acme/x"),
        ("Command: REPO acme/x", "REPO acme/x"),
        ("Output => ISSUES acme/x open", "ISSUES acme/x open"),
        ("=> ISSUES acme/x open", "ISSUES acme/x open"),
        ('"ISSUES acme/x"', "ISSUES acme/x"),
        ("`SEARCH pumps`", "SEARCH pumps"),
        ("\n\nREPO acme/x\nextra explanation", "REPO acme/x"),
        ("NONE", None),
        ("none.", None),
        ("", None),
    ],
)
def test_model_lines_are_normalised_before_parsing(raw, expected):
    assert normalize_interpretation(raw) == expected


def test_open_issue_is_a_synonym_for_new_issue():
    assert parse("open issue acme/x Login broken -- since today") == CreateIssue(
        "acme/x", "Login broken", "since today"
    )


def fake_llm(content=None, status=200, payload=None):
    def handler(request):
        assert request.url.path.endswith("/chat/completions")
        assert request.headers["Authorization"] == "Bearer k"
        body = payload if payload is not None else {"choices": [{"message": {"content": content}}]}
        return httpx.Response(status, json=body)

    return LLM("https://llm.test/v1", "k", "m", transport=httpx.MockTransport(handler))


def test_interpret_round_trips_through_the_client():
    assert fake_llm("```\nNEW ISSUE acme/x T\n```").interpret("open an issue") == "NEW ISSUE acme/x T"


@pytest.mark.parametrize(
    ("status", "payload"),
    [(500, {}), (200, {"choices": []}), (200, {"choices": [{"message": {"content": "  "}}]}), (200, {"nope": 1})],
)
def test_provider_failures_raise_llm_error(status, payload):
    with pytest.raises(LLMError):
        fake_llm(status=status, payload=payload).interpret("x")
