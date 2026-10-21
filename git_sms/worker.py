"""Background processing for the durable queues in the store.

Delivery policy, stated once:
- Inbound messages are at-least-once: a message is queued durably before Twilio gets
  its 200, and one interrupted mid-processing is processed again after a restart.
  That is safe because every write is journalled against the message's MessageSid and
  never repeated; at worst a read is answered twice.
- Each sender's messages are processed one at a time, in arrival order.
- Replies are at-most-once once a send is in flight: a send that Twilio certainly did not
  accept (429, or a connection that never opened) is retried, bounded, and its segments
  refunded; a send whose fate is unknown (a timeout, any 5xx, an interrupted process) is
  never resent. One sender's replies are handed to Twilio one at a time, in order.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Protocol

from git_sms.config import Settings
from git_sms.service import Inbound, Service
from git_sms.sms import segments
from git_sms.store import Job, OutboundMessage

log = logging.getLogger(__name__)
MAX_PROCESSING_ATTEMPTS = 3
MAX_SEND_ATTEMPTS = 3
SWEEP_INTERVAL_SECONDS = 600


class SendError(Exception):
    """A send that did not succeed. `retry`: certainly not accepted, try again. `ambiguous`: may have been."""

    def __init__(self, message: str, *, retry: bool = False, ambiguous: bool = False):
        super().__init__(message)
        self.retry = retry
        self.ambiguous = ambiguous


class Sender(Protocol):
    def send(self, *, to: str, from_: str, body: str, status_callback: str | None = None) -> str | None: ...


class Worker:
    def __init__(self, service: Service, sender: Sender, settings: Settings, status_callback: str | None = None):
        self.service = service
        self.store = service.store
        self.sender = sender
        self.settings = settings
        self.status_callback = status_callback
        self._wake = threading.Event()
        self._stopping = threading.Event()
        self._threads: list[threading.Thread] = []
        self._last_sweep = 0.0

    # Lifecycle -----------------------------------------------------------------------

    def start(self) -> None:
        recovered = self.store.recover()
        if any(recovered.values()):
            log.warning("Recovered after restart: %s", recovered)
        for index in range(self.settings.workers):
            thread = threading.Thread(target=self._loop, name=f"git-sms-worker-{index}", daemon=True)
            thread.start()
            self._threads.append(thread)

    def stop(self, timeout: float = 10.0) -> None:
        self._stopping.set()
        self._wake.set()
        for thread in self._threads:
            thread.join(timeout)
        self._threads.clear()

    def wake(self) -> None:
        self._wake.set()

    def _loop(self) -> None:
        while not self._stopping.is_set():
            try:
                self.maybe_sweep()
                if not self.step():
                    self._wake.wait(1.0)
                    self._wake.clear()
            except Exception:
                log.exception("Worker iteration failed")
                time.sleep(1.0)

    def drain(self) -> None:
        """Process everything that is ready, in the calling thread (tests and one-shot runs)."""
        while self.step():
            pass

    def maybe_sweep(self) -> None:
        if self.store.now() - self._last_sweep >= SWEEP_INTERVAL_SECONDS:
            self._last_sweep = self.store.now()
            removed = self.store.sweep(self.settings.retention_days, self.settings.audit_retention_days)
            if any(removed.values()):
                log.info("Retention sweep removed %s", removed)

    def step(self) -> bool:
        # Replies first: a send is quick and bounded, and a waiting reply (often a confirmation
        # code with a deadline) must not queue behind other senders' inbound work.
        message = self.store.claim_outbound()
        if message is not None:
            self._send(message)
            return True
        job = self.store.claim_inbound()
        if job is not None:
            self._process(job)
            return True
        return False

    def alive(self) -> bool:
        return bool(self._threads) and all(thread.is_alive() for thread in self._threads)

    # Inbound -------------------------------------------------------------------------

    def _process(self, job: Job) -> None:
        payload = job.payload
        inbound = Inbound(
            job.message_sid,
            payload["from"],
            payload["to"],
            payload["body"],
            payload.get("num_media", 0),
            payload.get("opt_out_type"),
        )
        try:
            replies = self.service.handle(inbound)
        except Exception:
            log.exception("Processing failed (attempt %d)", job.attempts)
            if job.attempts >= MAX_PROCESSING_ATTEMPTS:
                self.store.fail_inbound(job)
            else:
                self.store.requeue_inbound(job)
            return
        self.store.complete_inbound(
            job, [({"to": inbound.sender, "from": inbound.recipient, "body": body}, segments(body)) for body in replies]
        )

    # Outbound ------------------------------------------------------------------------

    def _send(self, message: OutboundMessage) -> None:
        budget_key = "segments:all"
        if self.store.consume(budget_key, 86400, message.segments) > self.settings.max_segments_per_day:
            self.store.consume(budget_key, 86400, -message.segments)
            self.store.settle_outbound(message.id, "failed", error="daily segment budget exhausted")
            self.store.audit(
                sender_key=message.sender_key,
                message_sid=message.message_sid,
                action="send",
                outcome="budget_exhausted",
            )
            log.error("Daily SMS segment budget exhausted; reply not sent")
            return
        payload = message.payload
        try:
            provider_sid = self.sender.send(
                to=payload["to"], from_=payload["from"], body=payload["body"], status_callback=self.status_callback
            )
        except SendError as error:
            if error.ambiguous:
                self.store.settle_outbound(message.id, "unknown", error=str(error))
                return
            self.store.consume(budget_key, 86400, -message.segments)
            if error.retry and message.attempts < MAX_SEND_ATTEMPTS:
                self.store.retry_outbound(message.id, 2.0**message.attempts, str(error))
            else:
                self.store.settle_outbound(message.id, "failed", error=str(error))
            return
        except Exception as error:
            log.exception("Unexpected sender failure")
            self.store.settle_outbound(message.id, "unknown", error=type(error).__name__)
            return
        self.store.settle_outbound(message.id, "accepted", provider_sid=provider_sid)
