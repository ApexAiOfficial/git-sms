# Testing

Git SMS is tested in layers, from fast and fully offline to slow and fully real. Each layer proves something the
previous one cannot.

## 1. Unit and integration tests (`pytest`)

Offline, a few seconds. GitHub is a scripted `httpx.MockTransport`, the model is a fake, the clock is
controllable, and webhooks carry real signatures computed with the Twilio SDK. They cover:

- the grammar, including every carrier keyword, malformed input and canonical round-trips;
- the sender directory and repository-grant matching (including owner-boundary wildcards);
- GSM-7/UCS-2 segment counting and pagination that always fits the budget;
- the store: one-time intake under concurrency, per-sender ordering, crash recovery, the outbound ledger, the
  write journal, atomic confirmation, retention sweeps, sealing bound to the sender, and a byte-level check that
  no phone number, message text or code reaches the database file;
- GitHub auth (token and App JWT, token caching) and error mapping;
- service behaviour end to end: authorisation, confirmation codes (wrong, expired, replaced, bound to the
  sender, grants re-checked), limits, opt-outs, guests, paging, summaries and natural-language safety;
- the HTTP edge and worker: forged, unsigned, tampered, wrong-URL and wrong-account webhooks are rejected before
  queueing; redelivery is processed once; each crash window (after the 200, mid-processing, after the GitHub
  write, mid-write with and without evidence on GitHub, and by fault injection right after the confirmation is
  accepted and right after the attempt starts) recovers without loss or a repeated write; transient,
  permanent and ambiguous send failures are handled distinctly; status callbacks are verified; Advanced Opt-Out
  messages are never answered.

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

## 3. Crash-recovery end-to-end run (`e2e/crash_recovery_e2e.py`)

Same environment as above, but the harness starts the server itself. It gets a confirmation code, queues a burst
of reads followed by the `CONFIRM` from one sender, `SIGKILL`s the server as soon as the last webhook is
acknowledged, restarts it on the same database, and checks that every acknowledged message is answered, that the
confirmation is answered once, and, through the GitHub API, that exactly one issue carrying its reconciliation
marker exists. It also checks that the kill really left work unfinished, so a pass is meaningful.

Both harnesses simulate Twilio with correctly signed requests and read replies from the development outbox. They
prove the server and GitHub legs, not carrier delivery.

## 4. Carrier-delivered runs (`e2e/live_sms_e2e.py`)

Real SMS through carrier networks in both directions, the real Git SMS core (service, store, worker) in the harness
process, and a real sandbox repository. The flow is `WHOAMI`, `REPO`, `NEW ISSUE`, `CONFIRM`; the issue is then
checked on GitHub, and every message is checked against an independent record. Only messages the harness itself
sent are processed, so an automatic acknowledgement can never start a reply loop.

- **`--path android`: an Android handset as Git SMS's SMS gateway, with no SMS provider in Git SMS.** The handset
  runs Termux with Termux:API and `e2e/phone/git-sms-gate.sh` as the forced command of one SSH key: it can only
  text allowlisted numbers and only return the conversation with them (filtered on the phone), with no shell.
  Inbound messages are polled from it and replies leave from its own SIM. A Twilio number plays the user, driven
  through an external harness's public API, and the handset's replies are confirmed in Twilio's message log.
- **`--path twilio`: Git SMS on a Twilio number.** Replies go out through Git SMS's real `TwilioSender`, and
  Twilio reports each one `delivered`. Inbound messages are read from Twilio's message log, because the test
  number's webhook belongs to another application; the webhook leg itself is what the signed-webhook run covers.
  A handset plays the user, sending through its gate and reading replies from its own inbox.

Results on 2026-10-23, with a private sandbox repository: both paths passed every check (android 8/8, twilio 9/9,
twice each), with a median of about 13 seconds from sending a command to receiving its reply, in four to six
SMS per direction per run. Building them found three real defects, all fixed with regression tests:

- the gate's body pattern used a regex repeat bound of 2200, which Android's C library rejects (bionic caps
  `RE_DUP_MAX` at 255, glibc does not), so on the phone every reply was refused. `tests/test_phone_gate.py`
  checks that no gate pattern exceeds 255;
- the gate piped `termux-sms-list -h` into `grep -q`, which can kill the writer with SIGPIPE and fails under
  `pipefail`, so inbox reads were refused at random;
- Termux:API ignores `-t inbox` when a sender filter is given and returns both directions of the conversation,
  so a gateway would see its own replies as new commands. The handset client now keeps only received messages.

Setup for the handset: install the gate and a dedicated key (see the comments in `e2e/phone/git-sms-gate.sh`),
put the counterpart's number in `~/.config/git-sms-gate/allow`, and allow Termux:API's SMS permission. The run
needs `GITHUB_TOKEN`, `TWILIO_ACCOUNT_SID`/`TWILIO_AUTH_TOKEN`, `TWILIO_NUMBER`, `HANDSET_NUMBER`, `HANDSET_SSH`,
`HANDSET_KEY`, and for `--path android` `HARNESS_SMS_API_URL`/`HARNESS_SMS_API_KEY` (an external API that can send
an SMS from the Twilio number). Phone numbers are never printed or written to the evidence file.

Still not covered: Twilio calling Git SMS's own webhook with a carrier-delivered message, which needs a dedicated
number pointed at a public URL, and a number registered for US application traffic (A2P 10DLC or toll-free
verification) of its own. Without registration, replies to US numbers fail with Twilio error 30034.

## 5. Live AI run (`e2e/live_ai_e2e.py`)

Asks for the provider key with a hidden prompt (never echoed, logged or stored), starts a server with a daily AI
budget of six calls, and checks: a repository summary; a summary of an issue that tries a prompt injection (it must
not be obeyed); a free-form read, which must show its interpretation; a free-form write, which must stop at a
confirmation code; an attempt to approve by free text, which must not write; chatter, which must do nothing; and a
seventh call, which the budget must refuse without calling the model. GitHub is checked to prove no AI path wrote.

Against DeepSeek (`deepseek-flash`, OpenAI-compatible, `--base-url https://api.deepseek.com`) on 2026-10-23 the
first run passed 8/9: the model's rewrite of "please open an issue in ... titled ..." did not parse. The rewrite
prompt gained worked examples, model output is now normalised (code fences, quotes, labels), and `OPEN ISSUE`
became a synonym for `NEW ISSUE`; the rerun passed 9/9, at one to five seconds per model call.

Keep live tests isolated: a private disposable repository, fictional or dedicated numbers, and spend bounded by
`GIT_SMS_MAX_SEGMENTS_PER_DAY` and `GIT_SMS_MAX_AI_CALLS_PER_DAY`.
