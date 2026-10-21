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
| A redelivered webhook, a crash or a retry repeats a write | Each `MessageSid` is queued once. Every write is journalled against the message that triggered it before it is attempted, and carries a hidden `<!-- git-sms:... -->` marker, so re-processing reports or reconciles the earlier write instead of repeating it. |
| A write's outcome is unknown (timeout, dropped connection, GitHub 5xx) | Recorded as `unknown` and never retried automatically. The sender is told to check GitHub first, and an identical later request is flagged as a possible duplicate before it can be confirmed. |
| Messages from one sender processed out of order | One sender's messages run strictly in arrival order; different senders run in parallel. |
| Unknown sender | Operator-owned directory; unknown numbers get no reply (or read-only public access if `public_read` is enabled, using an anonymous GitHub client and no AI). |
| Spoofed sender number | Every write needs a one-time 4-digit code sent to the registered number, bound to that sender, valid for 10 minutes, dropped after 3 wrong tries, and checked and consumed in one atomic step. A spoofer never sees the code. Production cannot disable confirmation. |
| Over-broad access | Per-user repository grants for read and write, applied to every read (search results included) and re-checked at confirmation; a GitHub App installation (or fine-grained token) bounds the credential itself. Pull request state changes are refused. |
| Prompt injection via the language model | The model only proposes one line, which is re-parsed by the deterministic grammar and authorised like typed input. It cannot confirm, and any write it proposes always needs the code. Repository content sent for summaries is delimited as untrusted data and the output is only text. |
| Abuse and runaway cost | Per-sender hourly message limit (one notice, then silence), per-sender daily write limit, a global daily outbound-segment ceiling (refunded when a send certainly failed), a global daily AI call budget, no AI for guests, paging instead of long messages, and no reply to opt-out keywords or Twilio's `OptOutType` messages. |
| Slow dependencies | Webhooks are acknowledged as soon as they are queued; GitHub, model and Twilio calls have bounded timeouts. A reply whose send may have reached Twilio is never resent. |

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
  the outcome stays `unknown`, which is the safe answer.
- Delivery receipts depend on Twilio's status callbacks reaching `GIT_SMS_PUBLIC_URL/twilio/status`.
- Git SMS has not yet been validated against real carrier traffic; see [docs/TESTING.md](docs/TESTING.md).
