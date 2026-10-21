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
            **overrides,
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
    assert e.service.store.outbound_for("SM" + "a" * 32) == [("accepted", "SMout1", None, 1)]


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
    e.service.store.begin_operation(op_id, job.sender_key, "CreateIssue", "acme/field-reports", None, "fp")
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
    e.service.store.begin_operation(op_id, job.sender_key, "CreateIssue", "acme/field-reports", None, "fp")
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
    assert e.service.store.outbound_for(sid(1)) == [("failed", None, "twilio 400 21610", 1)]


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
    assert e.service.store.outbound_for(sid(1)) == [("undelivered", "SMout1", "twilio 30034", 1)]


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
