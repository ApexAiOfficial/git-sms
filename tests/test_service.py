"""Behaviour of one message end to end through the service, against a fake GitHub."""

import json

from git_sms.service import COMMANDS_TEXT, NOT_UNDERSTOOD

from .conftest import ALICE, BOB, STRANGER, Conversation, FakeLLM

WRITE_PATH = "POST /repos/acme/field-reports/issues"


def chat(service, phone=ALICE) -> Conversation:
    return Conversation(service, phone)


# Discovery and access ----------------------------------------------------------------


def test_commands_and_forwarded_help_keyword(make_service):
    alice = chat(make_service())
    assert alice.send("commands") == COMMANDS_TEXT
    assert alice.send("HELP") == COMMANDS_TEXT
    assert alice.send("commands close").startswith("CLOSE owner/repo#12")


def test_unknown_senders_get_silence_and_no_github_calls(make_service, api):
    service = make_service()
    assert chat(service, STRANGER).send("repo acme/field-reports") == ""
    assert api.requests == []
    assert service.store.audit_log()[0].action == "unknown_sender"


def test_whoami(make_service):
    assert chat(make_service()).send("whoami") == ("You are alice. Read: *. Write: acme/field-*. Default repo: none.")


def test_reader_is_confined_to_granted_repos(make_service, api):
    bob = chat(make_service(), BOB)
    assert "acme/field-reports" in bob.send("repo acme/field-reports")
    assert bob.send("repo other/secret") == "You don't have access to other/secret."
    assert "GET /repos/other/secret" not in api.calls()


# Reads -------------------------------------------------------------------------------


def test_repo_overview(make_service):
    reply = chat(make_service()).send("repo acme/field-reports")
    assert reply.splitlines()[0] == "acme/field-reports"
    assert "Stars 7, forks 2" in reply
    assert reply.endswith("https://github.com/acme/field-reports")


def test_issue_list_excludes_pull_requests(make_service):
    reply = chat(make_service()).send("issues acme/field-reports")
    assert reply.splitlines() == ["acme/field-reports open issues:", "#12 Pump 3 leaks (2c)", "#11 Gate"]


def test_issue_detail_and_pull_request_redirect(make_service):
    alice = chat(make_service())
    reply = alice.send("issue acme/field-reports#12")
    assert reply.startswith("acme/field-reports#12 [open] Pump 3 leaks")
    assert "by maria, 2 comments, labels: bug" in reply
    assert alice.send("issue acme/field-reports#13") == "#13 is a pull request. Text PR acme/field-reports#13."


def test_pull_requests(make_service):
    alice = chat(make_service())
    assert alice.send("prs acme/field-reports") == "acme/field-reports open PRs:\n#13 Fix pump driver by dev [draft]"
    reply = alice.send("pr acme/field-reports#13")
    assert "fix-pump -> main" in reply and "+12 -3 in 2 files, mergeable: yes" in reply


def test_search(make_service):
    assert chat(make_service()).send("search pumps") == "1) acme/field-reports - Reports"


def test_default_repo_context(make_service):
    alice = chat(make_service())
    assert alice.send("issue #12") == "Which repo? Add owner/repo, or text USE owner/repo first."
    assert alice.send("use acme/field-reports").startswith("Default repo is now acme/field-reports")
    assert alice.send("issue #12").startswith("acme/field-reports#12")
    assert alice.send("whoami").endswith("Default repo: acme/field-reports.")


def test_use_requires_read_access(make_service, api):
    bob = chat(make_service(), BOB)
    assert bob.send("use other/secret") == "You don't have access to other/secret."
    assert api.requests == []


def test_github_errors_become_safe_replies(make_service, api):
    service = make_service()
    assert chat(service).send("issue acme/field-reports#404") == "Not found, or this service cannot see it."
    assert service.store.audit_log()[0].outcome == "github_error"


# Writes ------------------------------------------------------------------------------


def test_issue_creation_requires_the_confirmation_code(make_service, api):
    service = make_service()
    alice = chat(service)
    prompt = alice.send("new issue acme/field-reports Gate stuck -- The north gate will not close")
    assert prompt == (
        'Reply CONFIRM 4821 within 10 min to open an issue in acme/field-reports: "Gate stuck". ABORT drops it.'
    )
    assert api.calls("POST") == []

    assert alice.send("confirm 1111") == "Wrong code. Check the latest code and try again."
    assert api.calls("POST") == []

    assert (
        alice.send("confirm 4821") == "Created acme/field-reports#99: https://github.com/acme/field-reports/issues/99"
    )
    assert api.calls("POST") == [WRITE_PATH]
    sent = json.loads(api.requests[-1].content)
    assert sent["title"] == "Gate stuck"
    assert sent["body"].startswith("The north gate will not close")
    assert "Sent by alice via Git SMS" in sent["body"]
    assert ALICE not in sent["body"] and ALICE[-4:] not in sent["body"]

    assert alice.send("confirm 4821") == "Nothing is waiting for confirmation."
    assert api.calls("POST") == [WRITE_PATH]


def test_redelivered_confirmation_cannot_write_twice(make_service, api):
    service = make_service()
    alice = chat(service)
    alice.send("new issue acme/field-reports Once")
    alice.send("confirm 4821")
    alice.send("confirm 4821")
    assert api.calls("POST") == [WRITE_PATH]


def test_wrong_codes_drop_the_request(make_service, api):
    alice = chat(make_service())
    alice.send("new issue acme/field-reports Guess me")
    alice.send("confirm 0000")
    alice.send("confirm 0001")
    assert alice.send("confirm 0002") == "Wrong code too many times. The request was dropped."
    assert alice.send("confirm 4821") == "Nothing is waiting for confirmation."
    assert api.calls("POST") == []


def test_confirmation_expires(make_service, api, clock):
    alice = chat(make_service())
    alice.send("new issue acme/field-reports Later")
    clock.advance(601)
    assert alice.send("confirm 4821") == "That request expired. Send it again."
    assert api.calls("POST") == []


def test_abort_and_replacement(make_service, api):
    alice = chat(make_service())
    assert alice.send("abort") == "Nothing is waiting for confirmation."
    alice.send("new issue acme/field-reports First")
    assert "(replaces your earlier request)" in alice.send("new issue acme/field-reports Second")
    assert alice.send("abort") == "Dropped the change waiting for confirmation."
    assert alice.send("confirm 4821") == "Nothing is waiting for confirmation."
    assert api.calls("POST") == []


def test_codes_are_bound_to_the_sender(make_service, api):
    service = make_service()
    chat(service).send("new issue acme/field-reports Mine")
    assert chat(service, BOB).send("confirm 4821") == "Nothing is waiting for confirmation."
    assert api.calls("POST") == []


def test_write_authorization(make_service, api):
    service = make_service()
    assert chat(service, BOB).send("new issue acme/field-reports Nope") == "You can't change acme/field-reports."
    assert chat(service).send("comment acme/docs#1 Nope") == "You can't change acme/docs."
    assert api.calls("POST") == []


def test_grants_are_rechecked_at_confirmation(make_service, api):
    service = make_service()
    alice = chat(service)
    alice.send("new issue acme/field-reports Revoked")
    service.directory = type(service.directory).from_toml(
        '[[users]]\nphone = "' + ALICE + '"\nalias = "alice"\nread = ["*"]\n'
    )
    assert alice.send("confirm 4821") == "You can't change acme/field-reports."
    assert api.calls("POST") == []


def test_comment_with_default_repo(make_service, api):
    alice = chat(make_service())
    alice.send("use acme/field-reports")
    prompt = alice.send("comment #12 Fixed the seal")
    assert prompt.startswith(
        'Reply CONFIRM 4821 within 10 min to comment on issue acme/field-reports#12 "Pump 3 leaks"'
    )
    assert alice.send("confirm 4821").startswith("Commented on acme/field-reports#12: https://")
    body = json.loads(api.requests[-1].content)["body"]
    assert body.startswith("Fixed the seal") and "Sent by alice" in body


def test_close_describes_the_target_and_skips_no_ops(make_service, api):
    alice = chat(make_service())
    assert 'close issue acme/field-reports#12 "Pump 3 leaks"' in alice.send("close acme/field-reports#12")
    assert alice.send("confirm 4821") == "Closed acme/field-reports#12."
    assert json.loads(api.requests[-1].content) == {"state": "closed"}
    assert alice.send("reopen acme/field-reports#12") == "acme/field-reports#12 is already open."


def test_writes_without_confirmation_when_disabled(make_service, api):
    alice = chat(make_service(confirm_writes=False))
    assert alice.send("new issue acme/field-reports Direct").startswith("Created acme/field-reports#99")
    assert api.calls("POST") == [WRITE_PATH]


def test_daily_write_limit(make_service, api):
    alice = chat(make_service(confirm_writes=False, max_writes_per_day=1))
    alice.send("new issue acme/field-reports One")
    assert alice.send("new issue acme/field-reports Two") == "Daily change limit reached. Try again tomorrow."
    assert api.calls("POST") == [WRITE_PATH]


# Guests ------------------------------------------------------------------------------


def test_guests_read_anonymously_and_never_write(make_service, api):
    guest = chat(make_service(public_read=True), STRANGER)
    assert "acme/field-reports" in guest.send("repo acme/field-reports")
    assert "Authorization" not in api.requests[-1].headers
    assert guest.send("new issue acme/field-reports Spam") == "Guests can only read. Ask the operator for write access."
    assert api.calls("POST") == []


def test_registered_users_read_with_the_service_credential(make_service, api):
    chat(make_service()).send("repo acme/field-reports")
    assert api.requests[-1].headers["Authorization"] == "Bearer test-token"


# Limits, opt-outs, paging ------------------------------------------------------------


def test_hourly_message_limit_notifies_once(make_service, clock):
    alice = chat(make_service(max_commands_per_hour=2))
    alice.send("whoami")
    alice.send("whoami")
    assert alice.send("whoami") == "Message limit reached for this hour. Try again later."
    assert alice.send("whoami") == ""
    clock.advance(3600)
    assert alice.send("whoami").startswith("You are alice")


def test_opt_out_silences_until_opt_in(make_service, api):
    service = make_service()
    alice = chat(service)
    assert alice.send("STOP") == ""
    assert alice.send("repo acme/field-reports") == ""
    assert api.requests == []
    assert alice.send("START") == ""
    assert alice.send("whoami").startswith("You are alice")


def test_long_replies_page_with_more(make_service, api):
    api.add(
        "GET",
        "/repos/acme/field-reports/issues/12",
        {**json.loads(json.dumps(api.routes[("GET", "/repos/acme/field-reports/issues/12")][1])), "body": "x " * 400},
    )
    alice = chat(make_service(max_segments_per_reply=1))
    first = alice.send("issue acme/field-reports#12")
    assert first.endswith("MORE]") and first.count("[1/") == 1
    pages = [first]
    while (page := alice.send("more")) != "Nothing more to show.":
        pages.append(page)
    assert pages[-1].endswith(f"[{len(pages)}/{len(pages)}]")


# Summaries and natural language ------------------------------------------------------


def test_summaries_need_a_model(make_service):
    assert chat(make_service()).send("summarize acme/field-reports") == (
        "AI summaries are not set up here. Try REPO or ISSUE instead."
    )


def test_repository_and_thread_summaries(make_service):
    llm = FakeLLM(summary="Tracks field reports.")
    alice = chat(make_service(llm=llm))
    assert alice.send("summarize acme/field-reports") == "acme/field-reports: Tracks field reports."
    assert "README:\n# Field reports" in llm.summarized[-1][1]
    assert alice.send("summarize acme/field-reports issue") == "acme/field-reports#12: Tracks field reports."
    kind, thread = llm.summarized[-1]
    assert kind == "GitHub issue thread" and "lee: Seen it too." in thread


def test_natural_language_reads_show_the_interpretation(make_service):
    llm = FakeLLM(interpret="ISSUES acme/field-reports")
    reply = chat(make_service(llm=llm)).send("what is broken in the field reports?")
    assert reply.startswith("(ISSUES acme/field-reports open)\nacme/field-reports open issues:")


def test_natural_language_writes_always_need_confirmation(make_service, api):
    llm = FakeLLM(interpret="NEW ISSUE acme/field-reports Gate stuck -- reported by text")
    alice = chat(make_service(llm=llm, confirm_writes=False))
    assert alice.send("the north gate is stuck again").startswith("Reply CONFIRM 4821")
    assert api.calls("POST") == []


def test_natural_language_cannot_confirm_or_escape_the_grammar(make_service, api):
    service = make_service(llm=FakeLLM(interpret="CONFIRM 4821"))
    alice = chat(service)
    alice.send("new issue acme/field-reports Pending")
    assert alice.send("go ahead and approve it") == NOT_UNDERSTOOD
    assert api.calls("POST") == []
    service.llm = FakeLLM(interpret="DELETE REPO acme/field-reports")
    assert alice.send("delete everything") == NOT_UNDERSTOOD


def test_natural_language_respects_grants(make_service, api):
    llm = FakeLLM(interpret="REPO other/secret")
    assert (
        chat(make_service(llm=llm), BOB)
        .send("tell me about the secret repo")
        .endswith("You don't have access to other/secret.")
    )
    assert api.requests == []


# Privacy -----------------------------------------------------------------------------


def test_phone_numbers_never_reach_the_audit_log(make_service):
    service = make_service(confirm_writes=False)
    for phone in (ALICE, BOB, STRANGER):
        chat(service, phone).send("new issue acme/field-reports Audit me")
    entries = service.store.audit_log()
    assert entries
    dump = json.dumps([entry.__dict__ for entry in entries])
    for phone in (ALICE, BOB, STRANGER):
        assert phone not in dump and phone[1:] not in dump
