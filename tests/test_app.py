"""The HTTP edge, with real Twilio request signatures computed by the Twilio SDK."""

import pytest
from fastapi.testclient import TestClient
from twilio.request_validator import RequestValidator

from git_sms.app import EMPTY_TWIML, ConsoleSender, create_app
from git_sms.config import ConfigError, Settings

from .conftest import ALICE

TOKEN = "test-auth-token"
ACCOUNT = "AC" + "1" * 32
PUBLIC = "https://sms.example.org/git-sms"
OUR_NUMBER = "+15550100100"


class RecordingSender:
    def __init__(self, fail=False):
        self.sent = []
        self.fail = fail

    def send(self, *, to, from_, body):
        if self.fail:
            raise RuntimeError("provider down")
        self.sent.append((to, from_, body))
        return f"SM{len(self.sent)}"


@pytest.fixture
def edge(make_service):
    def build(**overrides):
        settings = Settings(
            environment="development",
            sender_backend="console",
            public_url=PUBLIC,
            twilio_auth_token=TOKEN,
            twilio_account_sid=ACCOUNT,
            **overrides,
        )
        service = make_service(**overrides)
        sender = RecordingSender()
        return TestClient(create_app(settings, service=service, sender=sender)), service, sender

    return build


def form(body, sid="SM" + "a" * 32, sender=ALICE, account=ACCOUNT):
    return {
        "MessageSid": sid,
        "SmsSid": sid,
        "AccountSid": account,
        "From": sender,
        "To": OUR_NUMBER,
        "Body": body,
        "NumMedia": "0",
    }


def post(client, params, path="/twilio/sms", url=None, signature=None):
    signature = signature or RequestValidator(TOKEN).compute_signature(url or PUBLIC + path, params)
    return client.post(path, data=params, headers={"X-Twilio-Signature": signature})


def test_signed_message_is_acknowledged_and_answered(edge):
    client, service, sender = edge()
    response = post(client, form("whoami"))
    assert response.status_code == 200
    assert response.text == EMPTY_TWIML
    assert response.headers["content-type"].startswith("application/xml")
    assert sender.sent == [(ALICE, OUR_NUMBER, "You are alice. Read: *. Write: acme/field-*. Default repo: none.")]
    assert service.store.inbound_status("SM" + "a" * 32) == "done"


def test_original_webhook_path_still_works(edge):
    client, _, sender = edge()
    assert post(client, form("whoami"), path="/webhook").status_code == 200
    assert len(sender.sent) == 1


@pytest.mark.parametrize(
    "mutate",
    [
        lambda params, sig: (params, None),  # unsigned
        lambda params, sig: (params, "bm90LWEtc2lnbmF0dXJl"),  # wrong signature
        lambda params, sig: ({**params, "Body": "new issue acme/field-reports Forged"}, sig),  # tampered body
        lambda params, sig: ({**params, "From": "+15550100002"}, sig),  # spoofed sender
    ],
)
def test_unauthenticated_requests_are_rejected_before_processing(edge, mutate):
    client, service, sender = edge()
    params = form("whoami")
    signature = RequestValidator(TOKEN).compute_signature(PUBLIC + "/twilio/sms", params)
    params, signature = mutate(params, signature)
    headers = {"X-Twilio-Signature": signature} if signature else {}
    response = client.post("/twilio/sms", data=params, headers=headers)
    assert response.status_code == 403
    assert sender.sent == []
    assert service.store.inbound_status(params["MessageSid"]) is None


def test_signature_for_another_url_is_rejected(edge):
    client, _, sender = edge()
    params = form("whoami")
    response = post(client, params, url="https://attacker.example/twilio/sms")
    assert response.status_code == 403
    assert sender.sent == []


def test_other_twilio_accounts_are_rejected(edge):
    client, _, sender = edge()
    assert post(client, form("whoami", account="AC" + "2" * 32)).status_code == 403
    assert sender.sent == []


def test_missing_fields_are_bad_requests(edge):
    client, _, _ = edge()
    params = form("whoami")
    del params["MessageSid"]
    assert post(client, params).status_code == 400


def test_redelivery_is_processed_once(edge, api):
    client, _, sender = edge(confirm_writes=False)
    params = form("new issue acme/field-reports Once only")
    assert post(client, params).status_code == 200
    assert post(client, params).status_code == 200
    assert api.calls("POST") == ["POST /repos/acme/field-reports/issues"]
    assert len(sender.sent) == 1


def test_daily_segment_budget_stops_sending(edge):
    client, service, sender = edge(max_segments_per_day=1)
    post(client, form("whoami", sid="SM" + "1" * 32))
    post(client, form("whoami", sid="SM" + "2" * 32))
    assert len(sender.sent) == 1
    assert service.store.audit_log()[0].outcome == "budget_exhausted"


def test_sender_failures_mark_the_message_failed(make_service):
    settings = Settings(environment="development", sender_backend="console")
    service = make_service()
    client = TestClient(create_app(settings, service=service, sender=RecordingSender(fail=True)))
    assert client.post("/twilio/sms", data=form("whoami")).status_code == 200
    assert service.store.inbound_status("SM" + "a" * 32) == "failed"


def test_development_without_a_token_accepts_unsigned_requests(make_service, tmp_path):
    outbox = tmp_path / "outbox.jsonl"
    settings = Settings(environment="development", sender_backend="console")
    client = TestClient(create_app(settings, service=make_service(), sender=ConsoleSender(str(outbox))))
    assert client.post("/twilio/sms", data=form("whoami")).status_code == 200
    assert '"body": "You are alice.' in outbox.read_text()


def test_production_refuses_to_start_without_secrets():
    with pytest.raises(ConfigError, match="TWILIO_AUTH_TOKEN"):
        create_app(Settings())


def test_health_and_no_public_docs(edge):
    client, _, _ = edge()
    assert client.get("/healthz").json()["status"] == "ok"
    assert client.get("/docs").status_code == 404
    assert client.get("/openapi.json").status_code == 404
