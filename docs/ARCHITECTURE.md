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
- **The command channel itself was broken on real numbers.** Twilio answers `HELP` and `INFO` itself and never
  forwards them, so the prototype's `help` command could not be reached. `YES`, `START`, `STOP`, `CANCEL`, `END` and
  `QUIT` change a subscriber's opt-out state, so they cannot be command words either.
- **Synchronous, unbounded replies.** Summaries ran inside the webhook against Twilio's 15-second timeout, replies
  could run to 1,000 characters (seven segments, or 15 if one curly quote forced UCS-2), and nothing deduplicated
  redelivered webhooks.
- **The language model routed actions.** Free text went to a model that picked an action and could guess a
  repository from a search result: unacceptable once writes exist.

## Design

```text
Twilio ──POST /twilio/sms──▶ app.py
                              1. verify X-Twilio-Signature (public URL) and AccountSid
                              2. claim MessageSid once (SQLite)          ── duplicate? ack, stop
                              3. ack with empty TwiML immediately
                              4. background: service.handle(message)
                                    opt-out keywords ─▶ record, stay silent
                                    sender directory ─▶ unknown: silent
                                    hourly limit     ─▶ one notice, then silent
                                    parse (deterministic grammar; optional LLM rewrite → re-parse)
                                    repository grant (read / write)
                                    reads  ─▶ GitHub REST ─▶ SMS text
                                    writes ─▶ code to the real handset ─▶ CONFIRM ─▶ re-check ─▶ GitHub
                                    audit every decision; paginate for MORE
                              5. daily segment budget ─▶ Twilio REST send
```

| Module | Responsibility |
| --- | --- |
| `config.py` | Settings from the environment; refuses unsafe production configuration |
| `identity.py` | TOML sender directory: E.164 numbers, aliases, read/write repository patterns, optional guests |
| `commands.py` | The grammar: one typed command per message; reserved-keyword handling; canonical rendering |
| `service.py` | Policy and orchestration: limits, authorisation, confirmation, reads, writes, audit, paging |
| `github.py` | REST client with GitHub App or token auth, bounded timeouts, SMS-safe error messages |
| `llm.py` | Optional OpenAI-compatible model for summaries and for rewriting free text into the grammar |
| `sms.py` | GSM-7 normalisation, segment counting, pagination that always fits the segment budget |
| `store.py` | SQLite: inbound idempotency, counters, pending confirmations, context, opt-outs, audit |
| `app.py` | FastAPI edge, Twilio validation, background processing, senders (Twilio REST, console) |

## Decisions

1. **Deterministic grammar first, model second.** The model may only rewrite text into one grammar line, which
   is parsed and authorised like typed input. It can never confirm, and writes it proposes always require the
   code. This keeps natural language useful without making it an authority.
2. **Confirmation codes on every write by default.** Beyond preventing accidents, the code is sent to the
   registered handset, so a spoofed `From` number cannot complete a write. One pending change per sender, a
   10-minute TTL, three attempts, and an atomic take that executes once.
3. **Acknowledge, then reply over the REST API.** Removes the webhook timeout from the critical path, allows
   paging and future multi-message flows, and lets the MessageSid claim guarantee at-most-once processing.
4. **SQLite, one process.** The workload is a few messages a minute. One file to back up keeps the service
   independently deployable anywhere. Every store method is a small transaction, so moving to PostgreSQL later
   is mechanical if it is ever needed.
5. **Least-privilege GitHub identity.** GitHub App installation tokens are the recommended path; user grants
   are checked on top, so a misconfigured directory still cannot exceed the installation.
6. **No phone numbers at rest or on GitHub.** Senders are keyed hashes in the database and aliases on GitHub.
7. **Official Twilio SDK for signature validation and sending**, rather than re-implementing request signing.
8. **httpx for GitHub and the model** (not provider SDKs): one small dependency, explicit timeouts, and
   `MockTransport` makes every API interaction testable without the network.

## Roadmap

1. **Carrier-verified operation.** A dedicated sender with toll-free verification or A2P 10DLC registration,
   Twilio status callbacks for delivery tracking, and a recorded live run from a real handset.
2. **Operator experience.** A GitHub App manifest helper, an audit export command, retention settings, and an
   optional per-user PIN for writes.
3. **Automation.** Allowlisted `workflow_dispatch` (`RUN deploy staging`) with per-user grants, confirmation, and
   run-status follow-up by SMS; subscriptions that text watched issue and pull-request updates.
4. **Reach.** Localised replies, and an optional Android SMS-gateway transport for communities that have a phone
   and a data connection but no SMS provider account.
