"""The HTTP edge and worker, with real Twilio request signatures computed by the Twilio SDK.

The worker is not started; tests drain it explicitly, which makes every crash window
between "acknowledged" and "replied" reachable on purpose.
"""

import httpx
import pytest
from fastapi.testclient import TestClient
from twilio.request_validator import RequestValidator

from git_sms.app import EMPTY_TWIML, ConsoleSender, create_app
from git_sms.config import ConfigError, Settings
from git_sms.service import Service
from git_sms.store import Store
from git_sms.worker import SendError

from .conftest import ALICE

TOKEN = "test-auth-token"
ACCOUNT = "AC" + "1" * 32
PUBLIC = "https://sms.example.org/git-sms"
OUR_NUMBER = "+15550100100"
ISSUES = "POST /repos/acme/field-reports/issues"


class RecordingSender:
    def __init__(self, errors=()):
        self.sent = []
        self.errors = list(errors)
        self.callbacks = []

    def send(self, *, to, from_, body, status_callback=None):
        self.callbacks.append(status_callback)
        if self.errors:
            raise self.errors.pop(0)
        self.sent.append((to, from_, body))
        return f"SMout{len(self.sent)}"


class Edge:
    def __init__(self, settings, service, sender):
        self.settings, self.service, self.sender = settings, service, sender
        self.app = create_app(settings, service=service, sender=sender, start_worker=False)
        self.client = TestClient(self.app)
        self.worker = self.app.state.worker

    def post(self, params, path="/twilio/sms", url=None, signature=None):
        signature = signature or RequestValidator(TOKEN).compute_signature(url or PUBLIC + path, params)
        return self.client.post(path, data=params, headers={"X-Twilio-Signature": signature})

    def text(self, body, sid):
        assert self.post(form(body, sid=sid)).status_code == 200
        self.worker.drain()
        return self.sender.sent[-1][2]

    def restart(self, **overrides):
        """A new process over the same database file."""
        store = Store(self.settings.db_path, "k" * 32, clock=self.service.store._clock)
        service = Service(
            self.service.settings, store, self.service.directory, self.service.github, code_source=lambda: "4821"
        )
        return Edge(self.settings, service, overrides.get("sender", self.sender))


@pytest.fixture
def edge(make_service, tmp_path):
    def build(sender=None, **overrides):
        settings = Settings(
            environment="development",
            sender_backend="console",
            public_url=PUBLIC,
            twilio_auth_token=TOKEN,
            twilio_account_sid=ACCOUNT,
            db_path=str(tmp_path / "git-sms.db"),
            **{k: v for k, v in overrides.items() if k != "public_read"},
        )
        service = make_service(db_path=str(tmp_path / "git-sms.db"), **overrides)
        return Edge(settings, service, sender or RecordingSender())

    return build


def form(body, sid="SM" + "a" * 32, sender=ALICE, account=ACCOUNT, **extra):
    return {
        "MessageSid": sid,
        "SmsSid": sid,
        "AccountSid": account,
        "From": sender,
        "To": OUR_NUMBER,
        "Body": body,
        "NumMedia": "0",
        **extra,
    }


def sid(n):
    return f"SM{n:032d}"


# Ingress -------------------------------------------------------------------------------


def test_signed_message_is_acknowledged_then_answered(edge):
    e = edge()
    response = e.post(form("whoami"))
    assert response.status_code == 200
    assert response.text == EMPTY_TWIML
    assert response.headers["content-type"].startswith("application/xml")
    assert e.sender.sent == []  # acknowledged before any work
    assert e.service.store.inbound_status("SM" + "a" * 32) == "queued"
    e.worker.drain()
    assert e.sender.sent == [(ALICE, OUR_NUMBER, "You are alice. Read: *. Write: acme/field-*. Default repo: none.")]
    assert e.service.store.inbound_status("SM" + "a" * 32) == "done"
    assert e.service.store.outbound_for("SM" + "a" * 32) == [("accepted", "SMout1", None, 1, None)]


def test_original_webhook_path_still_works(edge):
    e = edge()
    assert e.post(form("whoami"), path="/webhook").status_code == 200
    e.worker.drain()
    assert len(e.sender.sent) == 1


@pytest.mark.parametrize(
    "mutate",
    [
        lambda params, sig: (params, None),  # unsigned
        lambda params, sig: (params, "bm90LWEtc2lnbmF0dXJl"),  # wrong signature
        lambda params, sig: ({**params, "Body": "new issue acme/field-reports Forged"}, sig),  # tampered body
        lambda params, sig: ({**params, "From": "+15550100002"}, sig),  # spoofed sender
    ],
)
def test_unauthenticated_requests_are_rejected_before_queueing(edge, mutate):
    e = edge()
    params = form("whoami")
    signature = RequestValidator(TOKEN).compute_signature(PUBLIC + "/twilio/sms", params)
    params, signature = mutate(params, signature)
    headers = {"X-Twilio-Signature": signature} if signature else {}
    assert e.client.post("/twilio/sms", data=params, headers=headers).status_code == 403
    e.worker.drain()
    assert e.sender.sent == []
    assert e.service.store.inbound_status(params["MessageSid"]) is None


def test_signature_for_another_url_is_rejected(edge):
    e = edge()
    assert e.post(form("whoami"), url="https://attacker.example/twilio/sms").status_code == 403


def test_other_twilio_accounts_are_rejected(edge):
    e = edge()
    assert e.post(form("whoami", account="AC" + "2" * 32)).status_code == 403


def test_missing_fields_are_bad_requests(edge):
    e = edge()
    params = form("whoami")
    del params["MessageSid"]
    assert e.post(params).status_code == 400


def test_redelivery_is_processed_once(edge, api):
    e = edge(confirm_writes=False)
    params = form("new issue acme/field-reports Once only")
    assert e.post(params).status_code == 200
    assert e.post(params).status_code == 200
    e.worker.drain()
    assert e.post(params).status_code == 200  # even after processing
    e.worker.drain()
    assert api.calls("POST") == [ISSUES]
    assert len(e.sender.sent) == 1


def test_development_unsigned_mode_is_explicit(make_service, tmp_path):
    outbox = tmp_path / "outbox.jsonl"
    settings = Settings(environment="development", sender_backend="console", allow_unsigned=True)
    app = create_app(settings, service=make_service(), sender=ConsoleSender(str(outbox)), start_worker=False)
    assert TestClient(app).post("/twilio/sms", data=form("whoami")).status_code == 200
    app.state.worker.drain()
    text = outbox.read_text()
    assert '"body": "You are alice.' in text
    assert ALICE not in text and OUR_NUMBER not in text  # the outbox holds no numbers


def test_production_refuses_to_start_without_secrets():
    with pytest.raises(ConfigError, match="TWILIO_AUTH_TOKEN"):
        create_app(Settings())


def test_health_and_no_public_docs(edge):
    e = edge()
    assert e.client.get("/healthz").json()["status"] == "ok"
    assert e.client.get("/docs").status_code == 404
    assert e.client.get("/openapi.json").status_code == 404


# Ordering --------------------------------------------------------------------------------


def test_one_senders_messages_are_processed_in_arrival_order(edge):
    e = edge()
    e.post(form("use acme/field-reports", sid=sid(1)))
    e.post(form("issue #12", sid=sid(2)))
    e.post(form("new issue Gate", sid=sid(3)))
    e.post(form("confirm 4821", sid=sid(4)))
    e.worker.drain()
    replies = [body for _, _, body in e.sender.sent]
    assert replies[0].startswith("Default repo is now")
    assert replies[1].startswith("acme/field-reports#12")
    assert replies[2].startswith("Reply CONFIRM 4821")
    assert replies[3].startswith("Created acme/field-reports#99")


# Crash windows (GS-01) ---------------------------------------------------------------------


def test_acknowledged_message_survives_a_crash_before_processing(edge, api):
    e = edge()
    e.post(form("new issue acme/field-reports Survives", sid=sid(1)))  # 200 sent, then the process dies
    fresh = e.restart()
    fresh.worker.store.recover()
    fresh.worker.drain()
    assert fresh.sender.sent[-1][2].startswith("Reply CONFIRM 4821")


def test_crash_while_processing_is_retried_after_restart(edge, api):
    e = edge()
    e.post(form("whoami", sid=sid(1)))
    assert e.service.store.claim_inbound() is not None  # claimed, then the process dies
    fresh = e.restart()
    assert fresh.worker.store.recover()["requeued"] == 1
    fresh.worker.drain()
    assert fresh.sender.sent[-1][2].startswith("You are alice")


def test_crash_after_a_github_write_never_repeats_it(edge, api):
    e = edge()
    e.text("new issue acme/field-reports Exactly once", sid(1))
    e.post(form("confirm 4821", sid=sid(2)))
    job = e.service.store.claim_inbound()
    # The worker runs the CONFIRM (the write reaches GitHub), then the process dies before completing the job.
    e.service.handle(_inbound(job))
    assert api.calls("POST") == [ISSUES]
    fresh = e.restart()
    fresh.worker.store.recover()
    fresh.worker.drain()
    assert api.calls("POST") == [ISSUES]  # re-processing the CONFIRM did not write again
    assert fresh.sender.sent[-1][2] == "Already done: https://github.com/acme/field-reports/issues/99"


def test_crash_mid_write_is_reconciled_from_the_hidden_marker(edge, api):
    e = edge()
    e.text("new issue acme/field-reports Marked", sid(1))
    e.post(form("confirm 4821", sid=sid(2)))
    job = e.service.store.claim_inbound()
    op_id = e.service.store.op_id(job.message_sid)
    # The journal says "executing" and GitHub has the issue, but the process died before it heard back.
    e.service.store.record_intent(op_id, job.sender_key, "CreateIssue", "acme/field-reports", None, "fp", {})
    e.service.store.start_operation(op_id)
    marker_issue = {
        "number": 99,
        "html_url": "https://github.com/acme/field-reports/issues/99",
        "body": f"x\n<!-- git-sms:{op_id} -->",
    }
    api.add("GET", "/repos/acme/field-reports/issues", [marker_issue])
    fresh = e.restart()
    fresh.worker.store.recover()
    fresh.worker.drain()
    assert api.calls("POST") == []
    assert fresh.sender.sent[-1][2] == "Done: https://github.com/acme/field-reports/issues/99"


def test_crash_mid_write_without_evidence_is_reported_unknown_not_retried(edge, api):
    e = edge()
    e.text("new issue acme/field-reports Lost", sid(1))
    e.post(form("confirm 4821", sid=sid(2)))
    job = e.service.store.claim_inbound()
    op_id = e.service.store.op_id(job.message_sid)
    e.service.store.record_intent(op_id, job.sender_key, "CreateIssue", "acme/field-reports", None, "fp", {})
    e.service.store.start_operation(op_id)
    api.add("GET", "/repos/acme/field-reports/issues", [])
    fresh = e.restart()
    fresh.worker.store.recover()
    fresh.worker.drain()
    assert api.calls("POST") == []
    assert fresh.sender.sent[-1][2].startswith("GitHub did not confirm whether that change was made.")
    assert fresh.service.store.operation(op_id).status == "unknown"


def test_github_timeout_on_a_write_is_unknown_and_warns_before_a_resend(edge, api):
    def timeout(request):
        raise httpx.ReadTimeout("slow", request=request)

    e = edge()
    api.add("POST", "/repos/acme/field-reports/issues", timeout, status=201)
    e.text("new issue acme/field-reports Slow", sid(1))
    reply = e.text("confirm 4821", sid(2))
    assert reply.startswith("GitHub did not confirm whether that change was made. Check https://")
    assert "/acme/field-reports/issues before sending it again" in reply
    assert "Try again" not in reply
    again = e.text("new issue acme/field-reports Slow", sid(3))
    assert "an identical change at" in again and "unconfirmed outcome" in again


def test_a_completed_write_warns_before_an_identical_one(edge):
    e = edge()
    e.text("new issue acme/field-reports Twice", sid(1))
    e.text("confirm 4821", sid(2))
    reply = e.text("new issue acme/field-reports Twice", sid(3))
    assert "you already made this change at" in reply and "/issues/99" in reply


def test_processing_failures_retry_then_give_up(edge, monkeypatch):
    e = edge()
    calls = []

    def boom(inbound):
        calls.append(inbound.message_sid)
        raise RuntimeError("bug")

    monkeypatch.setattr(e.service, "handle", boom)
    e.post(form("whoami", sid=sid(1)))
    e.worker.drain()
    assert len(calls) == 3
    assert e.service.store.inbound_status(sid(1)) == "failed"


# Outbound (GS-06) --------------------------------------------------------------------------


def test_transient_send_failures_retry_and_refund_the_budget(edge, clock):
    sender = RecordingSender(errors=[SendError("twilio 429", retry=True)])
    e = edge(sender=sender)
    e.post(form("whoami", sid=sid(1)))
    e.worker.drain()
    assert e.service.store.outbound_for(sid(1))[0][0] == "pending"
    assert e.service.store.peek("segments:all", 86400) == 0
    clock.advance(3)
    e.worker.drain()
    assert e.service.store.outbound_for(sid(1))[0][:2] == ("accepted", "SMout1")


def test_ambiguous_send_failures_are_never_resent(edge, clock):
    sender = RecordingSender(errors=[SendError("ReadTimeout", ambiguous=True)])
    e = edge(sender=sender)
    e.post(form("whoami", sid=sid(1)))
    e.worker.drain()
    clock.advance(60)
    e.worker.drain()
    assert e.sender.sent == []
    assert e.service.store.outbound_for(sid(1))[0][0] == "unknown"


def test_permanent_send_failures_are_recorded(edge):
    e = edge(sender=RecordingSender(errors=[SendError("twilio 400 21610")]))
    e.post(form("whoami", sid=sid(1)))
    e.worker.drain()
    assert e.service.store.outbound_for(sid(1)) == [("failed", None, "twilio 400 21610", 1, None)]


def test_daily_segment_budget_stops_sending(edge):
    e = edge(max_segments_per_day=1)
    e.post(form("whoami", sid=sid(1)))
    e.post(form("whoami", sid=sid(2)))
    e.worker.drain()
    assert len(e.sender.sent) == 1
    assert e.service.store.outbound_for(sid(2))[0][2] == "daily segment budget exhausted"
    assert e.service.store.audit_log()[0].outcome == "budget_exhausted"


def test_delivery_status_callbacks_are_verified_and_recorded(edge):
    e = edge(sender=RecordingSender())
    e.post(form("whoami", sid=sid(1)))
    e.worker.drain()
    callback = {"MessageSid": "SMout1", "MessageStatus": "undelivered", "ErrorCode": "30034", "AccountSid": ACCOUNT}
    assert e.post(callback, path="/twilio/status", signature="Zm9yZ2Vk").status_code == 403
    assert e.post(callback, path="/twilio/status").status_code == 204
    assert e.service.store.outbound_for(sid(1)) == [("accepted", "SMout1", "twilio 30034", 1, "undelivered")]


def test_twilio_sends_ask_for_status_callbacks():
    from git_sms.app import create_app as factory  # noqa: F401 - import side effects only

    settings = Settings(
        environment="development",
        public_url=PUBLIC,
        twilio_auth_token=TOKEN,
        twilio_account_sid=ACCOUNT,
        sender_backend="twilio",
    )
    sender = RecordingSender()
    app = create_app(settings, sender=sender, start_worker=False, service=_service(settings))
    assert app.state.worker.status_callback == PUBLIC + "/twilio/status"


# Opt-out signals (GS-09) -------------------------------------------------------------------


@pytest.mark.parametrize(("opt_out_type", "body"), [("STOP", "stop"), ("HELP", "help"), ("START", "start")])
def test_advanced_opt_out_messages_are_never_answered(edge, opt_out_type, body):
    e = edge()
    e.post(form(body, sid=sid(1), OptOutType=opt_out_type))
    e.worker.drain()
    assert e.sender.sent == []


def test_advanced_opt_out_state_is_honoured(edge):
    e = edge()
    e.post(form("unsubscribe me please", sid=sid(1), OptOutType="STOP"))
    e.post(form("whoami", sid=sid(2)))
    e.worker.drain()
    assert e.sender.sent == []
    e.post(form("go", sid=sid(3), OptOutType="START"))
    e.post(form("whoami", sid=sid(4)))
    e.worker.drain()
    assert e.sender.sent[-1][2].startswith("You are alice")


# helpers ----------------------------------------------------------------------------------


def _inbound(job):
    from git_sms.service import Inbound

    p = job.payload
    return Inbound(job.message_sid, p["from"], p["to"], p["body"])


def _service(settings):
    from git_sms.identity import Directory

    from .conftest import USERS_TOML, standard_api

    return Service(settings, Store(":memory:", "k" * 32), Directory.from_toml(USERS_TOML), standard_api().client())


# Second review ------------------------------------------------------------------------------


class Crash(BaseException):
    """Stands in for the process dying at an exact instruction."""


def test_crash_between_confirmation_and_attempt_recovers_the_write(edge, api, monkeypatch):
    """P1-01: the confirmed command survives a crash right after the pending change is consumed."""
    e = edge()
    e.text("new issue acme/field-reports Confirmed then crashed", sid(1))
    e.post(form("confirm 4821", sid=sid(2)))

    def die(op_id):
        raise Crash

    monkeypatch.setattr(e.service.store, "start_operation", die)
    with pytest.raises(Crash):
        e.worker.drain()
    assert api.calls("POST") == []
    fresh = e.restart()
    fresh.worker.store.recover()
    fresh.worker.drain()
    assert api.calls("POST") == [ISSUES]
    assert fresh.sender.sent[-1][2].startswith("Created acme/field-reports#99")
    fresh.worker.store.recover()  # and a further re-run never writes again
    assert fresh.service.handle(_inbound_for(fresh, sid(2), "confirm 4821")) == [
        "Already done: https://github.com/acme/field-reports/issues/99"
    ]
    assert api.calls("POST") == [ISSUES]


def test_crash_after_the_attempt_started_is_never_retried(edge, api, monkeypatch):
    """The other side of P1-01: once an attempt may have reached GitHub, it is reconciled, not repeated."""
    e = edge()
    e.text("new issue acme/field-reports Maybe sent", sid(1))
    e.post(form("confirm 4821", sid=sid(2)))

    def die(*args, **kwargs):
        raise Crash

    monkeypatch.setattr(e.service.github, "create_issue", die)
    with pytest.raises(Crash):
        e.worker.drain()
    api.add("GET", "/repos/acme/field-reports/issues", [])
    fresh = e.restart()
    fresh.worker.store.recover()
    fresh.worker.drain()
    assert api.calls("POST") == []
    assert fresh.sender.sent[-1][2].startswith("GitHub did not confirm whether that change was made.")


def test_revoked_grants_stop_a_recovered_intent(edge, api, monkeypatch):
    e = edge()
    e.text("new issue acme/field-reports Revoked meanwhile", sid(1))
    e.post(form("confirm 4821", sid=sid(2)))
    monkeypatch.setattr(e.service.store, "start_operation", lambda op_id: (_ for _ in ()).throw(Crash()))
    with pytest.raises(Crash):
        e.worker.drain()
    fresh = e.restart()
    from git_sms.identity import Directory

    fresh.service.directory = Directory.from_toml('[[users]]\nphone = "' + ALICE + '"\nalias = "alice"\nread = ["*"]\n')
    fresh.worker.store.recover()
    fresh.worker.drain()
    assert api.calls("POST") == []
    assert fresh.sender.sent[-1][2] == "You can't change acme/field-reports."
    assert fresh.service.store.operation(fresh.service.store.op_id(sid(2))).status == "failed"


def test_replies_are_sent_before_more_inbound_is_processed(edge):
    """P2-05: a ready reply (often a confirmation code) never waits behind other senders' messages."""
    e = edge(public_read=True)
    e.post(form("whoami", sid=sid(1)))
    for n in range(20):
        e.post(form("whoami", sid=sid(100 + n), sender=f"+155501003{n:02d}"))
    e.worker.step()  # processes Alice's message
    e.worker.step()  # sends her reply, ahead of 20 queued messages
    assert [to for to, _, _ in e.sender.sent] == [ALICE]


def test_unknown_senders_never_take_a_queue_slot(edge):
    e = edge()
    assert e.post(form("whoami", sid=sid(1), sender="+15550100009")).status_code == 200
    assert e.service.store.inbound_status(sid(1)) is None
    assert e.service.store.audit_log()[0].action == "unknown_sender"


def test_a_full_queue_refuses_instead_of_acknowledging(edge):
    e = edge(max_queued=2)
    assert e.post(form("whoami", sid=sid(1))).status_code == 200
    assert e.post(form("whoami", sid=sid(2))).status_code == 200
    assert e.post(form("whoami", sid=sid(3))).status_code == 503
    assert e.service.store.inbound_status(sid(3)) is None


def test_readiness_reports_a_dead_worker_and_stalled_queues(make_service, tmp_path, clock):
    settings = Settings(environment="development", sender_backend="console", allow_unsigned=True)
    app = create_app(settings, service=make_service(), sender=RecordingSender(), start_worker=True)
    cold = TestClient(app).get("/readyz")  # lifespan not run: the worker never started
    assert cold.status_code == 503 and "worker not running" in cold.json()["problems"]
    with TestClient(app) as running:
        assert running.get("/readyz").status_code == 200
    e = Edge(
        Settings(environment="development", sender_backend="console", allow_unsigned=True),
        make_service(),
        RecordingSender(),
    )
    e.client.post("/twilio/sms", data=form("whoami", sid=sid(1)))
    clock.advance(121)
    stalled = e.client.get("/readyz")
    assert stalled.status_code == 503 and "inbound queue stalled" in stalled.json()["problems"]
    assert ALICE not in stalled.text


class _FakeMessages:
    def __init__(self, error):
        self.error = error

    def create(self, **kwargs):
        raise self.error


@pytest.mark.parametrize(
    ("status", "retry", "ambiguous"),
    [(429, True, False), (400, False, False), (500, False, True), (503, False, True)],
)
def test_twilio_rest_errors_map_to_safe_send_outcomes(status, retry, ambiguous):
    """P1/P2-02: a 5xx (503 included) may hide an accepted message, so it must never be resent."""
    from twilio.base.exceptions import TwilioRestException

    from git_sms.app import TwilioSender

    sender = TwilioSender(Settings(environment="development", twilio_account_sid=ACCOUNT, twilio_auth_token=TOKEN))
    sender._client = type("C", (), {"messages": _FakeMessages(TwilioRestException(status, "uri", code=1))})()
    with pytest.raises(SendError) as caught:
        sender.send(to=ALICE, from_=OUR_NUMBER, body="hi")
    assert (caught.value.retry, caught.value.ambiguous) == (retry, ambiguous)


@pytest.mark.parametrize(
    ("error", "retry", "ambiguous"), [("ConnectTimeout", True, False), ("ReadTimeout", False, True)]
)
def test_twilio_network_errors_map_to_safe_send_outcomes(error, retry, ambiguous):
    import requests

    from git_sms.app import TwilioSender

    sender = TwilioSender(Settings(environment="development", twilio_account_sid=ACCOUNT, twilio_auth_token=TOKEN))
    sender._client = type("C", (), {"messages": _FakeMessages(getattr(requests.exceptions, error)())})()
    with pytest.raises(SendError) as caught:
        sender.send(to=ALICE, from_=OUR_NUMBER, body="hi")
    assert (caught.value.retry, caught.value.ambiguous) == (retry, ambiguous)


def _inbound_for(edge_, message_sid, body):
    from git_sms.service import Inbound

    return Inbound(message_sid, ALICE, OUR_NUMBER, body)


# Third review ------------------------------------------------------------------------------


def test_an_unproven_state_change_stays_uncertain_in_state_audit_and_replies(edge, api, monkeypatch):
    """P2: finding the issue closed proves nothing about this request, now or on any later replay."""
    e = edge()
    e.text("close acme/field-reports#12", sid(1))
    e.post(form("confirm 4821", sid=sid(2)))

    def die(*args, **kwargs):
        raise Crash

    monkeypatch.setattr(e.service.github, "set_state", die)
    with pytest.raises(Crash):
        e.worker.drain()
    api.add("GET", "/repos/acme/field-reports/issues/12", {**ISSUE_12_CLOSED})  # someone else closed it meanwhile
    fresh = e.restart()
    fresh.worker.store.recover()
    fresh.worker.drain()
    op_id = fresh.service.store.op_id(sid(2))
    unproven = "acme/field-reports#12 is closed now, but GitHub did not confirm whether this request did it"
    assert fresh.sender.sent[-1][2].startswith(unproven)
    assert fresh.service.store.operation(op_id).status == "observed"
    assert any(entry.outcome == "observed_unproven" for entry in fresh.service.store.audit_log())
    replay = fresh.service.handle(_inbound_for(fresh, sid(2), "confirm 4821"))
    assert replay[0].startswith(unproven) and not replay[0].startswith("Already done")
    assert api.calls("PATCH") == []


def test_a_full_queue_still_acknowledges_a_redelivery(edge):
    e = edge(max_queued=1)
    assert e.post(form("whoami", sid=sid(1))).status_code == 200
    assert e.post(form("whoami", sid=sid(2))).status_code == 503
    assert e.post(form("whoami", sid=sid(1))).status_code == 200  # Twilio retrying something we already hold


ISSUE_12_CLOSED = {
    "number": 12,
    "title": "Pump 3 leaks",
    "state": "closed",
    "user": {"login": "maria"},
    "html_url": "https://github.com/acme/field-reports/issues/12",
}
