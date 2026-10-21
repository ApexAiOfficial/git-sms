# Security

## Reporting a vulnerability

Please report security issues privately through
[GitHub private vulnerability reporting](https://github.com/ApexAiOfficial/git-sms/security/advisories/new)
rather than in a public issue.

## Threat model

Git SMS turns text messages into GitHub actions, so the questions are: who is really texting, what may they
change, and what happens when something misbehaves. Controls, in the order each message meets them:

| Threat | Control |
| --- | --- |
| Forged webhook (anyone can POST to a public URL) | `X-Twilio-Signature` verified with the account auth token against the configured public URL; the `AccountSid` must match. Production refuses to start without these. |
| Redelivered or replayed webhook causing a second write | Each `MessageSid` is claimed once in SQLite before any work; repeats are acknowledged and ignored. Confirmations are taken atomically and execute at most once. |
| Unknown sender | Operator-owned directory; unknown numbers get no reply (or read-only public access if `public_read` is enabled, using an anonymous GitHub client). |
| Spoofed sender number | Every write needs a one-time 4-digit code sent to the registered number, bound to that sender, valid for 10 minutes, dropped after 3 wrong tries. A spoofer never sees the code. |
| Over-broad access | Per-user repository grants for read and write, re-checked at confirmation; a GitHub App installation (or fine-grained token) bounds the credential itself. |
| Prompt injection via the language model | The model only proposes one line, which is re-parsed by the deterministic grammar and authorised like typed input. It cannot confirm, and any write it proposes always needs the code. Repository content sent for summaries is delimited as untrusted data and the output is only text. |
| Abuse and runaway cost | Per-sender hourly message limit (one notice, then silence), per-sender daily write limit, global daily outbound-segment ceiling, paging instead of long messages, and no reply to opt-out keywords. |
| Personal data exposure | Phone numbers are stored only as HMAC-SHA256 keyed hashes; GitHub sees the alias, never the number; logs carry no numbers or message bodies outside development. |
| Slow dependencies | Webhooks are acknowledged immediately and processed in the background; GitHub, model and Twilio calls all have bounded timeouts. |

## Known limits

- SMS is not end-to-end encrypted: carriers and Twilio can read message content. Do not text secrets.
- A stolen or SIM-swapped phone can confirm writes within its grants. Keep write grants narrow; an optional
  per-user PIN is on the roadmap.
- Processing is in-process: if the server stops mid-message, that message is marked `received` and is not
  retried automatically (by design, so a write is never repeated). The sender can simply resend.
- The SQLite file holds the audit log and pending confirmations. Protect and back up its directory.
