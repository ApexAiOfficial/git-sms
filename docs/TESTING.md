# Testing

Git SMS is tested in layers, from fast and fully offline to slow and fully real. Each layer proves something the
previous one cannot.

## 1. Unit and integration tests (`pytest`)

Offline, a few seconds. GitHub is a scripted `httpx.MockTransport`, the model is a fake, the clock is
controllable, and webhooks carry real signatures computed with the Twilio SDK. They cover:

- the grammar, including every carrier keyword, malformed input and canonical round-trips;
- the sender directory and repository-grant matching (including owner-boundary wildcards);
- GSM-7/UCS-2 segment counting and pagination that always fits the budget;
- the store: one-time MessageSid claims under concurrency, fixed windows, at-most-once confirmations, and a
  byte-level check that no phone number reaches the database file;
- GitHub auth (token and App JWT, token caching) and error mapping;
- service behaviour end to end: authorisation, confirmation codes (wrong, expired, replaced, bound to the
  sender, grants re-checked), limits, opt-outs, guests, paging, summaries and natural-language safety;
- the HTTP edge: forged, unsigned, tampered, wrong-URL and wrong-account webhooks are rejected before any work;
  redelivery is processed once; the spend ceiling stops sending.

## 2. Signed-webhook end-to-end run (`e2e/signed_webhook_e2e.py`)

A real server process, a real (disposable, private) GitHub repository, and real Twilio-format signed requests.
Replies are captured from the development outbox; every effect is verified independently through the GitHub
API. It checks that nothing is written before confirmation, that a wrong code is refused, that the confirmed
issue, comment and close land exactly once (including after a redelivered confirmation), that attribution uses
the alias and never the number, that writes outside a grant and unknown senders are refused, and that forged and
unsigned webhooks write nothing.

```bash
# server: development mode with signatures enforced, console sender with an outbox
export GIT_SMS_ENV=development GIT_SMS_SENDER=console GIT_SMS_DEV_OUTBOX=/tmp/e2e/outbox.jsonl \
  GIT_SMS_USERS_FILE=/tmp/e2e/users.toml GIT_SMS_DB_PATH=/tmp/e2e/git-sms.db \
  GIT_SMS_PUBLIC_URL=http://127.0.0.1:8765 PORT=8765 \
  TWILIO_AUTH_TOKEN=$E2E_TWILIO_AUTH_TOKEN TWILIO_ACCOUNT_SID=$E2E_TWILIO_ACCOUNT_SID \
  GITHUB_TOKEN=<token that can write the sandbox repo>
python -m git_sms &

# harness: users.toml must grant +15550100001 read and write on the sandbox repo
python e2e/signed_webhook_e2e.py --url http://127.0.0.1:8765 --repo your-org/git-sms-sandbox \
  --outbox /tmp/e2e/outbox.jsonl --evidence /tmp/e2e/evidence.json
```

GitHub's list endpoints can lag a write by a few seconds; the harness polls them before concluding.

## 3. Carrier-delivered runs

The last legs (handset → carrier → Twilio → webhook, and the reply back) need a real Twilio number, a public HTTPS
URL, and a sender registered for US traffic (toll-free verification or A2P 10DLC). Without registration, inbound
messages and the GitHub actions they trigger work, but replies to US numbers fail with Twilio error 30034, and are
still billed. Useful drivers, all external to Git SMS:

- **An SMS-capable test harness** that can send to the Git SMS number and record replies (any messaging platform
  with an API works, including a second Twilio number registered for A2P).
- **A physical Android handset** (for example Termux with Termux:API's `termux-sms-send`), reached through a
  forced-command SSH key that can only text the Git SMS number. This gives true carrier-delivered coverage.
- **Twilio's message logs** (`GET /2010-04-01/Accounts/{sid}/Messages.json`) as the independent record of inbound
  receipt and outbound delivery status.

Keep live tests isolated: a private disposable repository, fictional or dedicated numbers, and spend bounded by
`GIT_SMS_MAX_SEGMENTS_PER_DAY`.
