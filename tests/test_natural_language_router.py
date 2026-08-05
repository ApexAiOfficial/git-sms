import json

import natural_language_router


def test_natural_language_create_repo_still_disabled(monkeypatch):
    monkeypatch.setattr(
        natural_language_router,
        "complete_text",
        lambda **_: json.dumps({"action": "create_repo", "repo_name": "demo"}),
    )
    assert (
        natural_language_router.route_natural_command("create a repo called demo")
        == "Repository creation is disabled in this proof of concept."
    )


def test_natural_language_create_issue_still_disabled(monkeypatch):
    monkeypatch.setattr(
        natural_language_router,
        "complete_text",
        lambda **_: json.dumps(
            {
                "action": "create_issue",
                "repo": "owner/repo",
                "title": "Bug",
                "body": "Details",
            }
        ),
    )
    assert (
        natural_language_router.route_natural_command("file a bug in owner/repo")
        == "Issue creation is disabled in this proof of concept."
    )


def test_parse_command_naturally_rejects_unsupported_action(monkeypatch):
    monkeypatch.setattr(
        natural_language_router,
        "complete_text",
        lambda **_: json.dumps({"action": "delete_repo", "repo": "owner/repo"}),
    )
    assert natural_language_router.parse_command_naturally("delete it") == {"action": "unknown"}


def test_route_natural_command_rejects_non_string_repo(monkeypatch):
    monkeypatch.setattr(
        natural_language_router,
        "complete_text",
        lambda **_: json.dumps({"action": "create_issue", "repo": ["owner/repo"]}),
    )
    assert natural_language_router.route_natural_command("file an issue") is None
