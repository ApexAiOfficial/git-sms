"""HTTP edge: verify Twilio's signature, queue the message durably, answer at once.

The message (sealed) is committed to the intake queue before Twilio receives its 200,
so an acknowledged message survives a crash; the worker processes it in the
background and replies over the Twilio REST API. A redelivered webhook with the same
MessageSid is acknowledged and ignored. Twilio's delivery-status callbacks are
verified the same way and recorded against the outbound ledger.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from git_sms import __version__
from git_sms.config import Settings
from git_sms.github import AppAuth, GitHub, TokenAuth
from git_sms.identity import Directory
from git_sms.llm import LLM
from git_sms.service import Service
from git_sms.sms import segments
from git_sms.store import Store
from git_sms.worker import Sender, SendError, Worker

log = logging.getLogger(__name__)
EMPTY_TWIML = '<?xml version="1.0" encoding="UTF-8"?><Response></Response>'
STALL_SECONDS = 120


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

    def send(self, *, to: str, from_: str, body: str, status_callback: str | None = None) -> str | None:
        import requests
        from twilio.base.exceptions import TwilioRestException

        route = (
            {"messaging_service_sid": self._messaging_service_sid} if self._messaging_service_sid else {"from_": from_}
        )
        if status_callback:
            route["status_callback"] = status_callback
        try:
            return self._client.messages.create(to=to, body=body, **route).sid
        except TwilioRestException as error:
            detail = f"twilio {error.status} {error.code or ''}".strip()
            if error.status == 429:  # rate limited: rejected before processing
                raise SendError(detail, retry=True) from error
            # Any 5xx may hide an accepted message, so it is never resent; 4xx are definite refusals.
            raise SendError(detail, ambiguous=error.status >= 500) from error
        except requests.exceptions.ConnectTimeout as error:
            raise SendError("connect timeout", retry=True) from error
        except requests.exceptions.RequestException as error:
            raise SendError(type(error).__name__, ambiguous=True) from error


class ConsoleSender:
    """Development sender: logs replies and optionally appends them (without numbers) to a JSONL outbox."""

    def __init__(self, outbox: str | None = None):
        self._outbox = outbox
        self._lock = threading.Lock()

    def send(self, *, to: str, from_: str, body: str, status_callback: str | None = None) -> str | None:
        log.info("console reply (%d segments): %s", segments(body), body)
        if self._outbox:
            with self._lock, open(self._outbox, "a", encoding="utf-8") as handle:
                handle.write(json.dumps({"at": time.time(), "body": body}) + "\n")
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
    start_worker: bool = True,
) -> FastAPI:
    settings = settings or Settings.from_env()
    settings.validate()
    service = service or build_service(settings)
    sender = sender or build_sender(settings)
    validator = None
    if settings.twilio_auth_token:
        from twilio.request_validator import RequestValidator

        validator = RequestValidator(settings.twilio_auth_token)
    else:
        log.warning("GIT_SMS_ALLOW_UNSIGNED is on: webhooks are NOT authenticated. Development only.")
    status_callback = (
        f"{settings.public_url}/twilio/status" if settings.public_url and settings.sender_backend == "twilio" else None
    )
    worker = Worker(service, sender, settings, status_callback)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        if start_worker:
            worker.start()
        try:
            yield
        finally:
            if start_worker:
                worker.stop()

    app = FastAPI(
        title="Git SMS", version=__version__, docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan
    )
    app.state.service = service
    app.state.worker = worker

    async def verified_params(request: Request) -> dict[str, str] | None:
        form = await request.form()
        params = {key: str(value) for key, value in form.multi_items()}
        if validator is not None:
            signature = request.headers.get("X-Twilio-Signature", "")
            if not signature or not validator.validate(signed_url(settings, request), params, signature):
                log.warning("Rejected a request with a missing or invalid Twilio signature")
                return None
        if settings.twilio_account_sid and params.get("AccountSid") != settings.twilio_account_sid:
            log.warning("Rejected a request for another Twilio account")
            return None
        return params

    async def twilio_sms(request: Request) -> Response:
        params = await verified_params(request)
        if params is None:
            return Response(status_code=403)
        message_sid, sender_number = params.get("MessageSid", ""), params.get("From", "")
        if not message_sid or not sender_number:
            return Response(status_code=400)
        try:
            num_media = int(params.get("NumMedia") or 0)
        except ValueError:
            num_media = 0
        if service.directory.lookup(sender_number) is None:
            # Not in the directory: never answered, so not worth a queue slot either.
            service.store.audit(
                sender_key=service.store.sender_key(sender_number),
                message_sid=message_sid,
                action="unknown_sender",
                outcome="ignored",
            )
            return Response(content=EMPTY_TWIML, media_type="application/xml")
        payload = {
            "from": sender_number,
            "to": params.get("To", ""),
            "body": params.get("Body", ""),
            "num_media": num_media,
            "opt_out_type": params.get("OptOutType") or None,
        }
        queued = service.store.enqueue_inbound(
            message_sid, service.store.sender_key(sender_number), payload, max_waiting=settings.max_queued
        )
        if queued == "full":
            log.error("Intake queue full (%d); refusing a webhook so Twilio records the failure", settings.max_queued)
            return Response(status_code=503)
        if queued == "queued":
            worker.wake()
        else:
            log.info("Ignored a redelivered message")
        return Response(content=EMPTY_TWIML, media_type="application/xml")

    async def twilio_status(request: Request) -> Response:
        params = await verified_params(request)
        if params is None:
            return Response(status_code=403)
        provider_sid, status = params.get("MessageSid", ""), params.get("MessageStatus", "")
        if provider_sid and status:
            service.store.record_delivery(provider_sid, status.lower()[:20], params.get("ErrorCode") or None)
        return Response(status_code=204)

    app.add_api_route("/twilio/sms", twilio_sms, methods=["POST"])
    app.add_api_route("/webhook", twilio_sms, methods=["POST"], include_in_schema=False)
    app.add_api_route("/twilio/status", twilio_status, methods=["POST"])

    @app.get("/healthz")
    def healthz() -> JSONResponse:
        """Liveness: the process answers."""
        return JSONResponse({"status": "ok", "version": __version__})

    @app.get("/readyz")
    def readyz() -> JSONResponse:
        """Readiness: the worker is running and the queues are moving. Counts only, no identifiers."""
        health = service.store.health()
        problems = []
        if start_worker and not worker.alive():
            problems.append("worker not running")
        if health["oldest_inbound_seconds"] > STALL_SECONDS:
            problems.append("inbound queue stalled")
        if health["oldest_reply_seconds"] > STALL_SECONDS:
            problems.append("replies stalled")
        body = {"status": "ready" if not problems else "not_ready", "problems": problems, **health}
        return JSONResponse(body, status_code=200 if not problems else 503)

    return app
