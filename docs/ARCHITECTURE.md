# Architecture

## Where this started

The original proof of concept (github/git-sms) showed the idea: a FastAPI webhook that answered SMS with
AI summaries of public repositories. The restoration branch proposed upstream as github/git-sms#1 made it import
and run again, bounded its requests, and added regression tests, while keeping writes disabled.

That baseline was sound where it touched GitHub reads, but it could not safely grow into the vision, because:

- **Anyone could impersonate anyone.** No Twilio signature check, and `From` was the only identity, so a single
  forged POST could act as any number.
- **No identity, authorisation or audit.** No sender directory, repository grants, confirmation or record of who
  did what. Writes had to stay disabled because nothing could make them safe.
- **The command channel itself was broken on real numbers.** With Twilio's default opt-out handling, `HELP` and
  `INFO` are answered by Twilio and not forwarded, so the prototype's `help` command could not be reached. `YES`, `START`, `STOP`, `CANCEL`, `END` and
  `QUIT` change a subscriber's opt-out state, so they cannot be command words either.
- **Synchronous, unbounded replies.** Summaries ran inside the webhook against Twilio's 15-second timeout, replies
  could run to 1,000 characters (seven segments, or 15 if one curly quote forced UCS-2), and nothing deduplicated
  redelivered webhooks.
- **Its AI provider no longer exists.** The prototype defaulted to GitHub Models, which GitHub retired on
  2026-07-30; its endpoint now answers `200 OK` in plain text, which the old code would have treated as a failure
  at best. Git SMS now speaks the OpenAI-compatible protocol to any provider and requires the model to be named.
- **The language model routed actions.** Free text went to a model that picked an action and could guess a
  repository from a search result: unacceptable once writes exist.

## Design

```text
Twilio ──POST /twilio/sms──▶ app.py (HTTP edge)
                              1. verify X-Twilio-Signature (public URL) and AccountSid
                              2. commit the message, sealed, to the intake queue (MessageSid unique)
                                                                    ── duplicate? ack, stop
                              3. ack with empty TwiML
        worker.py (thread pool, one sender at a time, arrival order)
                              4. claim the oldest queued message of an idle sender
                              5. service.handle(message)
                                    Twilio OptOutType / opt-out keywords ─▶ record, stay silent
                                    sender directory ─▶ unknown: silent
                                    hourly limit     ─▶ one notice, then silent
                                    parse (grammar; optional LLM rewrite → re-parse, daily AI budget)
                                    repository grant (read / write; search results filtered)
                                    reads  ─▶ GitHub REST ─▶ SMS text
                                    writes ─▶ code to the real handset ─▶ CONFIRM: take the code and journal
                                             the intent in one transaction ─▶ executing (once) ─▶ GitHub
                                             (issues and comments carry a hidden marker) ─▶ done | failed | unknown
                              6. commit replies to the outbound ledger, retire the message
                              7. send (replies before new inbound work; one in flight per sender, in order):
                                    daily segment budget ─▶ Twilio REST (SID recorded)
Twilio ──POST /twilio/status─▶ delivery status recorded against the SID (only ever advances)
```

| Module | Responsibility |
| --- | --- |
| `config.py` | Settings from the environment; refuses unsafe production configuration |
| `identity.py` | TOML sender directory: E.164 numbers, aliases, read/write repository patterns, optional guests |
| `commands.py` | The grammar: one typed command per message; reserved-keyword handling; canonical rendering |
| `service.py` | Policy: limits, authorisation, confirmation, reads, journalled writes, reconciliation, audit, paging |
| `worker.py` | Background processing: per-sender ordering, crash recovery, sending with retry, retention sweeps |
| `github.py` | REST client with GitHub App or token auth, bounded timeouts and paging, SMS-safe errors |
| `llm.py` | Optional OpenAI-compatible model for summaries and for rewriting free text into the grammar |
| `sms.py` | GSM-7 normalisation, segment counting, pagination that always fits the segment budget |
| `store.py` | SQLite: intake queue, write journal, outbound ledger, counters, confirmations, context, audit |
| `app.py` | FastAPI edge, Twilio validation, status callbacks, senders (Twilio REST, console) |

## Delivery guarantees

| What | Guarantee | How |
| --- | --- | --- |
| Acknowledged inbound message | At least once | Committed before the 200; re-queued after a crash; given up after 3 failed attempts |
| One sender's messages | Processed in arrival order | A sender's next message is claimed only when none of theirs is in progress |
| Confirmed write | Durably approved; not guaranteed to execute | The code is accepted and the write journalled as an approved intent in one transaction; the intent may still fail, end unknown, or, if its message is given up, wait as "approved, not attempted" for an operator |
| GitHub write | At most once | An intent moves to executing exactly once, before the call; re-processing reports or reconciles |
| Write GitHub did not confirm | Reported as unknown, never retried | Reconciled by its marker (issues, comments: `done`) or target state (state changes: `observed`, never `done`); operators settle the rest |
| Reply | At most once after a send starts | Retried only when Twilio certainly did not accept it (429, connect timeout); any 5xx is unknown |
| One sender's replies | Handed to Twilio in order, one at a time | A reply is claimed only when none of the same sender's earlier replies is waiting or in flight |
| Delivery status | Monotonic | Callbacks may repeat or arrive early or late; a status only moves forward |

At-least-once intake is safe because the only effects that matter, GitHub writes, are made at most once by the
journal; the worst case is a read answered twice after a crash.

## Decisions

1. **Deterministic grammar first, model second.** The model may only rewrite text into one grammar line, which
   is parsed and authorised like typed input. It can never confirm, and writes it proposes always require the
   code. This keeps natural language useful without making it an authority.
2. **Confirmation codes on every write, always in production.** Beyond preventing accidents, the code is sent to
   the registered handset, so a spoofed `From` number cannot complete a write. One pending change per sender, a
   10-minute TTL, three attempts, and one atomic compare-and-take.
3. **Queue durably, acknowledge, then reply over the REST API.** The webhook does one small transaction, which
   keeps it far inside Twilio's timeout and makes a crash after the 200 recoverable.
4. **Journal writes; never replay an uncertain one.** Issue and comment creation are not idempotent on GitHub.
   Each write is journalled as an intent in the same transaction that accepts its confirmation, attempted at most
   once, and keyed to its triggering message; issues and comments carry a marker that reconciliation can find,
   and an unconfirmed outcome is surfaced to the person who asked and to the operator.
5. **SQLite, one process, a worker pool.** The workload is a few messages a minute. One file to back up keeps the
   service independently deployable anywhere, and every store method is one transaction.
6. **Least-privilege GitHub identity.** GitHub App installation tokens are the recommended path; user grants are
   checked on top, including on search results, so a misconfigured directory still cannot exceed the
   installation and a sender never learns of repositories outside their grants.
7. **Seal what must be kept, delete it when done.** Queued messages, replies, pending changes and pages are
   AES-GCM encrypted under a key that never enters the database and expire on schedule; senders are keyed hashes.
8. **Official Twilio SDK for signature validation and sending**, rather than re-implementing request signing.
9. **httpx for GitHub and the model** (not provider SDKs): one small dependency, explicit timeouts, and
   `MockTransport` makes every API interaction testable without the network.

## Review record

An independent review of `9fa5797` (2026-10-21) raised these findings. Each confirmed one has a regression test
that fails on `9fa5797`.

| Finding | Verdict | Resolution |
| --- | --- | --- |
| GS-01 acknowledged messages lost; ambiguous writes | Confirmed | Durable sealed intake, crash recovery, write journal with markers and reconciliation; unit kill points plus a SIGKILL end-to-end run |
| GS-02 SEARCH reveals out-of-grant private repos | Confirmed | Results filtered by read grants before formatting |
| GS-03 production allows confirmation-free writes | Confirmed | Refused in production |
| GS-04 sensitive content kept without retention | Confirmed | Sealing, expiry, retention sweep, `0600` database, backup guidance |
| GS-05 "no phone numbers at rest" overstated | Confirmed (docs) | The users file, `.env` and user-written GitHub text are now named explicitly; the dev outbox no longer stores numbers |
| GS-06 no outbound ledger or delivery status | Confirmed | Ledger with Twilio SIDs, signed status callbacks, retry for certain failures, budget refunds |
| GS-07 same-sender reordering; confirm races | Confirmed | Per-sender FIFO claim; atomic compare-and-take |
| GS-08 no global AI budget | Confirmed | Global daily call budget; no AI for guests |
| GS-09 HELP forwarding overstated | Partly: true for default handling, not with Advanced Opt-Out | `OptOutType` honoured and never answered; docs say which configuration does what |
| GS-10 CLOSE/REOPEN on pull requests unproven | Confirmed | Pull request state changes refused; PR comment permissions documented as unverified |
| GS-11 dev mode and release hardening | Partly | Unsigned ingress needs an explicit dev-only flag; CI builds and installs the wheel and audits dependencies. Live reverse-proxy and Twilio validation remain carrier-test work |
| GS-14 first-page-only issue and comment reads | Confirmed | Bounded multi-page reads |
| Not in the review: a write timeout told the user "Try again" | Confirmed | Unknown-outcome reply, and a duplicate warning before an identical write |

A second review of `1f981ed` (2026-10-21) confirmed the first three were fixed and raised these; each has a
regression test (deterministic fault injection for P1-01) that fails on `1f981ed`.

| Finding | Verdict | Resolution |
| --- | --- | --- |
| P1-01 a crash between taking the confirmation and journalling the write loses it | Confirmed | One transaction turns the pending change into a journalled intent; intents are attempted once |
| P1/P2-02 a Twilio 503 was retried as certainly unaccepted | Confirmed | Every 5xx is treated as possibly accepted and never resent |
| P2-03 one sender's replies could be sent concurrently, out of order | Confirmed | Per-sender, in-order, one-in-flight send hand-off |
| P2-04 delivery callbacks could arrive early or regress a status | Confirmed | Monotonic delivery state, early callbacks kept until the SID is recorded |
| P2-05 inbound work could starve replies; unknown senders filled the queue | Confirmed | Replies first; unknown senders not queued; bounded queue refuses with 503 |
| P2-06 health said nothing about the worker or stuck work | Confirmed | `/readyz`, and `git-sms status`, `reconcile` and `resolve` |
| P2-07 "every write carries a marker" was too broad | Confirmed (docs and wording) | State changes are reconciled by state and reported as unproven |
| Not in the review: Twilio's `sending` status shared the send-lifecycle column | Confirmed | Delivery status moved to its own column; recovery and ordering ignore it |

A third review of `55e536a` (2026-10-21) raised two P2s; both reproduce on `55e536a` and have regressions.

| Finding | Verdict | Resolution |
| --- | --- | --- |
| An unproven state change was recorded as `done`, so a replay answered "Already done" | Confirmed | New terminal state `observed`, audited `observed_unproven`, reported as unconfirmed on every replay; only an operator's `resolve` makes it `done` |
| Queue capacity was checked apart from the insert | Confirmed as latent: in the documented single process the async handler does not yield between them, but two processes on one file overflow | Check and insert are one transaction (tested with four writers on one file) |
| Not in the review: a redelivered message got 503 while the queue was full | Confirmed | Duplicates are recognised before capacity |

The final acceptance review of `d7ed1e6` approved merging as a development foundation and raised one minor P2:
observed (unproven) state changes were missing from `git-sms status` and the health counts. Fixed in `89c342a`
with a regression that fails on `d7ed1e6`. Merged to `main` as `3e7ebb6` on 2026-10-22.

## Roadmap

Validation milestones come first; none has been done yet:

1. **Carrier-delivered SMS.** A dedicated sender with toll-free verification or A2P 10DLC registration, and a
   recorded run in both directions from a real handset, with Twilio's message log and delivery callbacks as
   independent evidence.
2. **A real GitHub App.** Install the least-privilege App described in SETUP.md on a test repository and observe
   every write, including whether commenting on a pull request needs more than the Issues permission.
3. **A live AI provider.** Exercise summaries and free-form routing against an OpenAI-compatible endpoint.

Then:

4. **Operator experience.** A GitHub App manifest helper, an audit export command, and an optional per-user PIN
   for writes.
5. **Automation.** Allowlisted `workflow_dispatch` (`RUN deploy staging`) with per-user grants, confirmation, and
   run-status follow-up by SMS; subscriptions that text watched issue and pull-request updates.
6. **Reach.** Localised replies, and an optional Android SMS-gateway transport for communities that have a phone
   and a data connection but no SMS provider account.
