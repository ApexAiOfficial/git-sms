"""HTTP edge: verify Twilio's signature, record the message once, answer at once,
then process it in the background and reply over the Twilio REST API.

Acknowledging immediately keeps every request far inside Twilio's 15-second webhook
timeout even when GitHub or the language model is slow, and the MessageSid claim
makes a redelivered webhook a no-op instead of a second GitHub write.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Protocol

from fastapi import BackgroundTasks, FastAPI, Request
from fastapi.responses import JSONResponse, Response

from git_sms import __version__
from git_sms.config import Settings
from git_sms.github import AppAuth, GitHub, TokenAuth
from git_sms.identity import Directory
from git_sms.llm import LLM
from git_sms.service import Inbound, Service
from git_sms.sms import segments
from git_sms.store import Store

log = logging.getLogger(__name__)
EMPTY_TWIML = '<?xml version="1.0" encoding="UTF-8"?><Response></Response>'


class Sender(Protocol):
    def send(self, *, to: str, from_: str, body: str) -> str | None: ...


class TwilioSender:
    def __init__(self, settings: Settings):
        from twilio.http.http_client import TwilioHttpClient
        from twilio.rest import Client

        http_client = TwilioHttpClient(timeout=15)
        if settings.twilio_api_key_sid and settings.twilio_api_key_secret:
            self._client = Client(
                settings.twilio_api_key_sid,
                settings.twilio_api_key_secret,
                settings.twilio_account_sid,
                http_client=http_client,
            )
        else:
            self._client = Client(settings.twilio_account_sid, settings.twilio_auth_token, http_client=http_client)
        self._messaging_service_sid = settings.twilio_messaging_service_sid

    def send(self, *, to: str, from_: str, body: str) -> str | None:
        route = (
            {"messaging_service_sid": self._messaging_service_sid} if self._messaging_service_sid else {"from_": from_}
        )
        return self._client.messages.create(to=to, body=body, **route).sid


class ConsoleSender:
    """Development sender: logs replies and optionally appends them to a JSONL outbox."""

    def __init__(self, outbox: str | None = None):
        self._outbox = outbox
        self._lock = threading.Lock()

    def send(self, *, to: str, from_: str, body: str) -> str | None:
        log.info("console reply (%d segments): %s", segments(body), body)
        if self._outbox:
            with self._lock, open(self._outbox, "a", encoding="utf-8") as handle:
                handle.write(json.dumps({"at": time.time(), "to": to, "from": from_, "body": body}) + "\n")
        return None


def build_service(settings: Settings) -> Service:
    if settings.github_app_id and settings.github_app_private_key and settings.github_app_installation_id:
        auth = AppAuth(
            settings.github_app_id,
            settings.github_app_private_key,
            settings.github_app_installation_id,
            settings.github_api_url,
        )
    elif settings.github_token:
        auth = TokenAuth(settings.github_token)
    else:
        auth = None
    llm = LLM(settings.llm_base_url, settings.llm_api_key, settings.llm_model) if settings.llm_configured else None
    return Service(
        settings,
        Store(settings.db_path, settings.effective_secret_key),
        Directory.load(settings.users_file),
        GitHub(settings.github_api_url, auth),
        guest_github=GitHub(settings.github_api_url, None),
        llm=llm,
    )


def build_sender(settings: Settings) -> Sender:
    return ConsoleSender(settings.dev_outbox) if settings.sender_backend == "console" else TwilioSender(settings)


def signed_url(settings: Settings, request: Request) -> str:
    """The URL Twilio signed: the configured public base plus this request's path and query."""
    if not settings.public_url:
        return str(request.url)
    query = request.url.query
    return f"{settings.public_url}{request.url.path}" + (f"?{query}" if query else "")


def create_app(
    settings: Settings | None = None,
    *,
    service: Service | None = None,
    sender: Sender | None = None,
) -> FastAPI:
    settings = settings or Settings.from_env()
    settings.validate()
    service = service or build_service(settings)
    sender = sender or build_sender(settings)
    validator = None
    if settings.twilio_auth_token:
        from twilio.request_validator import RequestValidator

        validator = RequestValidator(settings.twilio_auth_token)

    app = FastAPI(title="Git SMS", version=__version__, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.service = service
    app.state.sender = sender

    def process(inbound: Inbound) -> None:
        try:
            replies = service.handle(inbound)
            for body in replies:
                used = service.store.consume("segments:all", 86400, segments(body))
                if used > settings.max_segments_per_day:
                    log.error("Daily SMS segment budget exhausted; reply not sent")
                    service.store.audit(
                        sender_key=service.store.sender_key(inbound.sender),
                        message_sid=inbound.message_sid,
                        action="send",
                        outcome="budget_exhausted",
                    )
                    break
                sender.send(to=inbound.sender, from_=inbound.recipient, body=body)
            service.store.finish_inbound(inbound.message_sid, "done")
        except Exception:
            log.exception("Processing failed for an inbound message")
            service.store.finish_inbound(inbound.message_sid, "failed")

    async def twilio_sms(request: Request, background: BackgroundTasks) -> Response:
        form = await request.form()
        params = {key: str(value) for key, value in form.multi_items()}
        if validator is not None:
            signature = request.headers.get("X-Twilio-Signature", "")
            if not signature or not validator.validate(signed_url(settings, request), params, signature):
                log.warning("Rejected a webhook with a missing or invalid Twilio signature")
                return Response(status_code=403)
        if settings.twilio_account_sid and params.get("AccountSid") != settings.twilio_account_sid:
            log.warning("Rejected a webhook for another Twilio account")
            return Response(status_code=403)

        message_sid, sender_number = params.get("MessageSid", ""), params.get("From", "")
        if not message_sid or not sender_number:
            return Response(status_code=400)
        try:
            num_media = int(params.get("NumMedia") or 0)
        except ValueError:
            num_media = 0
        inbound = Inbound(message_sid, sender_number, params.get("To", ""), params.get("Body", ""), num_media)
        if service.store.claim_inbound(message_sid, service.store.sender_key(sender_number)):
            background.add_task(process, inbound)
        else:
            log.info("Ignored a redelivered message")
        return Response(content=EMPTY_TWIML, media_type="application/xml")

    app.add_api_route("/twilio/sms", twilio_sms, methods=["POST"])
    app.add_api_route("/webhook", twilio_sms, methods=["POST"], include_in_schema=False)

    @app.get("/healthz")
    def healthz() -> JSONResponse:
        return JSONResponse({"status": "ok", "version": __version__})

    return app
