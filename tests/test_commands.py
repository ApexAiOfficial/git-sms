import pytest

from git_sms.commands import (
    Abort,
    AddComment,
    Confirm,
    CreateIssue,
    Help,
    Invalid,
    Keyword,
    ListIssues,
    ListPulls,
    More,
    Ref,
    RepoInfo,
    Search,
    SetState,
    ShowIssue,
    ShowPull,
    Summarize,
    Unknown,
    Use,
    WhoAmI,
    canonical,
    from_record,
    parse,
    to_record,
)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("commands", Help()),
        ("?", Help()),
        ("COMMANDS issue", Help("issue")),
        ("help pulls", Help("prs")),
        ("whoami", WhoAmI()),
        ("more", More()),
        ("Next", More()),
        ("use acme/field-reports", Use("acme/field-reports")),
        ("use https://github.com/acme/field-reports/", Use("acme/field-reports")),
        ("repo", RepoInfo()),
        ("repo acme/docs", RepoInfo("acme/docs")),
        ("issues", ListIssues()),
        ("issues acme/docs closed", ListIssues("acme/docs", "closed")),
        ("ISSUES all", ListIssues(None, "all")),
        ("issue acme/docs#12", ShowIssue(Ref("acme/docs", 12))),
        ("issue #12", ShowIssue(Ref(None, 12))),
        ("issue acme/docs 12", ShowIssue(Ref("acme/docs", 12))),
        ("issue acme/docs #12", ShowIssue(Ref("acme/docs", 12))),
        ("prs", ListPulls()),
        ("pulls acme/docs", ListPulls("acme/docs")),
        ("pr #5", ShowPull(Ref(None, 5))),
        ("search internet in a box", Search("internet in a box")),
        ("summarize acme/docs", Summarize("acme/docs")),
        ("summarize acme/docs#4", Summarize("acme/docs", 4)),
        ("summarize acme/docs issue", Summarize("acme/docs", latest_issue=True)),
        ("summarize acme/docs issue 7", Summarize("acme/docs", 7)),
        ("summary", Summarize()),
        (
            "new issue acme/docs Broken link -- The FAQ link 404s",
            CreateIssue("acme/docs", "Broken link", "The FAQ link 404s"),
        ),
        ("new issue Broken link", CreateIssue(None, "Broken link", "")),
        ("NEW ISSUE acme/docs Title\nline two", CreateIssue("acme/docs", "Title", "line two")),
        ("create issue acme/docs Legacy form -- body", CreateIssue("acme/docs", "Legacy form", "body")),
        ("issue new acme/docs Alt form", CreateIssue("acme/docs", "Alt form", "")),
        ("comment acme/docs#3 Looks good to me", AddComment(Ref("acme/docs", 3), "Looks good to me")),
        ("comment #3 multi\nline", AddComment(Ref(None, 3), "multi\nline")),
        ("comment acme/docs 3 spaced form", AddComment(Ref("acme/docs", 3), "spaced form")),
        ("close #3", SetState(Ref(None, 3), "closed")),
        ("reopen acme/docs#3", SetState(Ref("acme/docs", 3), "open")),
        ("confirm 0042", Confirm("0042")),
        ("abort", Abort()),
        ("nevermind", Abort()),
    ],
)
def test_grammar(text, expected):
    assert parse(text) == expected


@pytest.mark.parametrize(
    "word", ["STOP", "stop", "Cancel", "END.", "quit", "unsubscribe", "stopall", "revoke", "optout"]
)
def test_opt_out_keywords_are_recognised_only_as_whole_messages(word):
    assert parse(word) == Keyword("opt_out")


@pytest.mark.parametrize("word", ["START", "yes", "Unstop"])
def test_opt_in_keywords(word):
    assert parse(word) == Keyword("opt_in")


@pytest.mark.parametrize("word", ["HELP", "info", "Help!"])
def test_carrier_help_keywords(word):
    assert parse(word) == Keyword("help")


def test_keywords_inside_sentences_are_ordinary_text():
    assert parse("stop the build please") == Unknown("stop the build please")
    assert isinstance(parse("comment #3 please stop"), AddComment)


@pytest.mark.parametrize(
    "text",
    [
        "use not-a-repo",
        "use acme/../etc",
        "use acme/..",
        "repo acme/docs extra",
        "issues acme/docs sideways",
        "issue acme/docs",
        "issue #0",
        "pr",
        "search",
        "summarize nope",
        "summarize acme/docs pulls",
        "new issue",
        "new issue acme/docs",
        "new thing",
        "comment #3",
        "comment acme/docs",
        "close",
        "close #3 now",
        "confirm",
        "confirm 12",
        "confirm abcd",
    ],
)
def test_malformed_commands_explain_usage(text):
    command = parse(text)
    assert isinstance(command, Invalid)
    assert command.usage


def test_bad_owner_names_are_not_repositories():
    assert isinstance(parse("use -acme/docs"), Invalid)
    assert isinstance(parse("use acme/docs;rm"), Invalid)


def test_empty_and_unknown():
    assert parse("") == Unknown("")
    assert parse("   ") == Unknown("")
    assert parse("what is going on") == Unknown("what is going on")


def test_write_flags():
    assert parse("new issue acme/docs T").is_write
    assert parse("comment #1 x").is_write
    assert parse("close #1").is_write
    assert not parse("issue #1").is_write
    assert not parse("confirm 1234").is_write


def test_long_fields_are_bounded():
    command = parse("new issue acme/docs " + "t" * 400 + " -- " + "b" * 3000)
    assert len(command.title) == 256
    assert len(command.body) == 1500


@pytest.mark.parametrize(
    "command",
    [
        CreateIssue("acme/docs", "Title", "Body"),
        AddComment(Ref("acme/docs", 3), "Hi"),
        SetState(Ref("acme/docs", 3), "closed"),
    ],
)
def test_write_records_round_trip(command):
    assert from_record(to_record(command)) == command


def test_unknown_record_types_are_rejected():
    with pytest.raises(KeyError):
        from_record({"type": "DeleteRepo", "repo": "acme/docs"})


@pytest.mark.parametrize(
    "text",
    [
        "issues acme/docs closed",
        "issue acme/docs#12",
        "prs acme/docs",
        "pr acme/docs#5",
        "search pumps",
        "summarize acme/docs#4",
        "summarize acme/docs issue",
        "new issue acme/docs Title -- Body",
        "comment acme/docs#3 Hello there",
        "close acme/docs#3",
        "reopen acme/docs#3",
        "repo acme/docs",
    ],
)
def test_canonical_form_reparses_to_the_same_command(text):
    command = parse(text)
    assert parse(canonical(command)) == command
