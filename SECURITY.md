# Security

## Reporting a vulnerability

Please do not open a public issue. Report vulnerabilities privately through
[GitHub private vulnerability reporting](https://github.com/ApexAiOfficial/git-sms/security/advisories/new) for this
repository.

## Threat model

Git SMS turns text messages into GitHub actions, so the questions are: who is really texting, what may they
change, what happens when something misbehaves, and what is kept afterwards. Controls, in the order each message
meets them:

| Threat | Control |
| --- | --- |
| Forged webhook (anyone can POST to a public URL) | `X-Twilio-Signature` verified with the account auth token against the configured public URL; the `AccountSid` must match. Production refuses to start without these; unsigned ingress needs an explicit, development-only `GIT_SMS_ALLOW_UNSIGNED`. Delivery-status callbacks are verified the same way. |
| An acknowledged message is lost in a crash | The message is committed (encrypted) to the SQLite intake queue before Twilio receives its 200. Messages interrupted mid-processing are re-queued on restart; one that keeps failing is given up after three attempts. |
| A crash silently discards a confirmed change | Accepting the confirmation code and journalling the write as an approved intent are one transaction, so after a crash the approval still exists. It is then attempted if no attempt had started, or reported as unknown if one might have. Durable approval is not guaranteed execution: an approved write can still fail (GitHub refuses it, grants were revoked) or end unknown, and an approval whose message was given up after repeated processing failures stays visible as "approved, not attempted" in `git-sms status` until an operator resolves it. |
| A redelivered webhook, a crash or a retry repeats a write | Each `MessageSid` is queued once. An approved intent moves to "executing" exactly once, just before GitHub is called, and is never attempted again: re-processing reports the result or reconciles it. New issues and comments carry a hidden `<!-- git-sms:... -->` marker that proves whether they landed. |
| A write's outcome is unknown (timeout, dropped connection, GitHub 5xx, a crash during the call) | Recorded as `unknown` and never retried automatically. The sender is told to check GitHub first, an identical later request is flagged as a possible duplicate, and operators can see and settle these writes with `git-sms status`, `reconcile` and `resolve`. |
| Messages or replies out of order | One sender's messages run strictly in arrival order, and their replies are handed to Twilio one at a time in the order written; different senders run in parallel. (Carriers do not guarantee delivery order.) |
| Unknown sender | Operator-owned directory; unknown numbers get no reply (or read-only public access if `public_read` is enabled, using an anonymous GitHub client and no AI). |
| Spoofed sender number | Every write needs a one-time 4-digit code sent to the registered number, bound to that sender, valid for 10 minutes, dropped after 3 wrong tries, and checked and consumed in one atomic step. A spoofer never sees the code. Production cannot disable confirmation. |
| Over-broad access | Per-user repository grants for read and write, applied to every read (search results included) and re-checked at confirmation; a GitHub App installation (or fine-grained token) bounds the credential itself. Pull request state changes are refused. |
| Prompt injection via the language model | The model only proposes one line, which is re-parsed by the deterministic grammar and authorised like typed input. It cannot confirm, and any write it proposes always needs the code. Repository content sent for summaries is delimited as untrusted data and the output is only text. |
| Abuse and runaway cost | Per-sender hourly message limit (one notice, then silence), per-sender daily write limit, a global daily outbound-segment ceiling (refunded when a send certainly failed), a global daily AI call budget, no AI for guests, paging instead of long messages, and no reply to opt-out keywords or Twilio's `OptOutType` messages. |
| Slow or failing dependencies | Webhooks are acknowledged as soon as they are queued; GitHub, model and Twilio calls have bounded timeouts. A reply whose send may have reached Twilio (any 5xx, a timeout, a crash mid-send) is never resent; only a 429 or a connection that never opened is retried. Delivery callbacks may repeat or arrive out of order: a status only ever advances. Replies are sent before more inbound work is taken, unknown senders never occupy the queue, and a full queue refuses webhooks (503) instead of acknowledging what it cannot hold. `/readyz` reports a stopped worker or a stalled queue. |

## What is stored, and where

- **The SQLite database** (`GIT_SMS_DB_PATH`, created `0600`). Senders appear only as HMAC-SHA256 keyed hashes.
  Everything that must briefly stay recoverable (a queued message with its number and text, a reply waiting to be
  sent, a pending change, `MORE` pages) is encrypted with AES-256-GCM under a key derived from
  `GIT_SMS_SECRET_KEY` and bound to the sender, and is deleted as soon as it is used. Confirmation codes are
  stored as HMACs. Pending changes expire after 10 minutes, pages after an hour, processed messages and settled
  replies after `GIT_SMS_RETENTION_DAYS` (30). The audit log, and writes whose outcome is still unknown, are kept
  for `GIT_SMS_AUDIT_RETENTION_DAYS` (365).
- **The users file** (`GIT_SMS_USERS_FILE`) holds real phone numbers, and `.env` holds the secrets. Keep both
  readable only by the service account.
- **GitHub** receives what users write (titles, bodies, comments), attributed to their alias rather than their
  number. What a user types can itself contain personal data; Git SMS does not scrub it.
- **Logs** carry no phone numbers or message bodies, except the development console sender, which logs replies.
  Its optional outbox file records reply text only.
- **The secret key** never touches the database. Losing it makes pending work unreadable (fail closed); rotating
  it starts fresh sender identities, opt-out state and context.

## Known limits

- SMS is not end-to-end encrypted: carriers and Twilio can read message content. Do not text secrets.
- If an AI model is configured, unrecognised messages and the repository or thread content being summarised
  (including private repositories the credential can read) are sent to that provider. Leave the model unset, or
  use a provider you trust with that data.
- A stolen or SIM-swapped phone can confirm writes within its grants. Keep write grants narrow; an optional
  per-user PIN is on the roadmap.
- Reconciliation looks for the write's marker on GitHub. If someone deletes the issue or edits the marker away,
  the outcome stays `unknown`, which is the safe answer. A state change (`CLOSE`, `REOPEN`) carries no marker:
  finding the issue in the target state records the write as `observed` (never `done`), audits it as
  `observed_unproven`, and every later reply says GitHub did not confirm this request did it, since someone else
  may have changed it. Only an operator's `resolve` turns it into `done`.
- If the process dies after a write was marked "executing" but before GitHub was called, nothing was written but
  the outcome is still reported as unknown. That is the price of never writing twice.
- Delivery receipts depend on Twilio's status callbacks reaching `GIT_SMS_PUBLIC_URL/twilio/status`.
- Git SMS has not yet been validated against real carrier traffic; see [docs/TESTING.md](docs/TESTING.md).
