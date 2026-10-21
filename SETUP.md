# Setting up Git SMS

You need a Twilio phone number, a GitHub credential, and a host that serves HTTPS. Git SMS is one small Python
process with one SQLite file, so any VPS, container platform or home server will do.

## 1. Install

```bash
git clone https://github.com/ApexAiOfficial/git-sms.git
cd git-sms
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt        # pinned, tested versions (Python 3.11+)
```

Or build the container: `docker build -t git-sms .`

## 2. Decide who may text it

Copy [users.example.toml](users.example.toml) somewhere private and list each person's number in E.164 form, an
alias (shown on GitHub instead of the number), and the repositories they may read and change:

```toml
[[users]]
phone = "+15550100001"
alias = "ana"
read = ["*"]
write = ["my-org/field-reports"]
```

Unknown numbers get no reply at all. Set `public_read = true` to let anyone read public repositories instead.

## 3. Give it a GitHub credential

**Recommended: a GitHub App.** Create one under *Settings > Developer settings > GitHub Apps* with webhooks
disabled and only these repository permissions: **Issues: read and write**, **Pull requests: read**,
**Contents: read**, **Metadata: read**. Install it on just the repositories people should reach, then set
`GITHUB_APP_ID`, `GITHUB_APP_INSTALLATION_ID` and `GITHUB_APP_PRIVATE_KEY_FILE`. Tokens are minted per hour and the
installation itself bounds what the service can ever touch.

**Simpler: a fine-grained personal access token** with the same permissions, limited to the same repositories,
as `GITHUB_TOKEN`. Avoid classic tokens: they reach every repository you can.

The user grants in step 2 are checked in addition to whatever the credential allows.

## 4. Configure

Copy [.env.example](.env.example) to `.env` (keep it private) and fill in at least:

| Variable | Purpose |
| --- | --- |
| `GIT_SMS_PUBLIC_URL` | The `https://` base URL Twilio calls. Signatures are checked against it. |
| `GIT_SMS_SECRET_KEY` | 32+ random characters (`python -c "import secrets; print(secrets.token_hex(32))"`). |
| `GIT_SMS_USERS_FILE` | Path to the file from step 2. |
| `GIT_SMS_DB_PATH` | Where the SQLite state lives (default `git-sms.db`). Back it up. |
| `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN` | Verify webhooks and send replies. |
| `TWILIO_API_KEY_SID`, `TWILIO_API_KEY_SECRET` | Optional: send with a revocable API key instead of the auth token. |
| `TWILIO_MESSAGING_SERVICE_SID` | Optional: reply through a Messaging Service instead of the receiving number. |

Optional: `LLM_BASE_URL` + `LLM_API_KEY` + `LLM_MODEL` for any OpenAI-compatible chat-completions endpoint (OpenAI,
Azure AI Foundry, a self-hosted vLLM or Ollama server...), or just `OPENAI_API_KEY` + `LLM_MODEL`, enables
`SUMMARIZE` and free-form messages. Name the model explicitly; there is no default. (GitHub Models, which the
original prototype used, was retired on 2026-07-30; its variables are now refused with an explanation.) Limits such as
`GIT_SMS_MAX_SEGMENTS_PER_DAY` are listed in `.env.example`.

The server refuses to start in production if anything security-relevant is missing.

## 5. Run

```bash
set -a; . ./.env; set +a
GIT_SMS_HOST=0.0.0.0 PORT=8000 python -m git_sms
```

Put it behind your HTTPS reverse proxy (or a tunnel) so that `GIT_SMS_PUBLIC_URL` + `/twilio/sms` reaches it.
`GET /healthz` reports liveness.

## 6. Point Twilio at it

In the Twilio Console, open the phone number, and under **Messaging > A message comes in** choose *Webhook*,
`HTTP POST`, `https://your-host.example/twilio/sms`. (`/webhook`, the prototype's path, also works.)

**US numbers must be registered to send.** Since 2023, US carriers block application SMS from unregistered
numbers: your users' messages will arrive, but replies fail with Twilio error 30034 until the number is on an
approved A2P 10DLC campaign, or is a verified toll-free number. Start that registration early; review can take
days to weeks. Inbound commands (including writes) work before it completes.

## 7. Try it

Text `COMMANDS` to your number, then `WHOAMI`, then `REPO owner/repo`.

## Local development

```bash
pip install -r requirements-dev.txt
pytest                     # unit and integration tests, no network
ruff check . && ruff format --check .
```

`GIT_SMS_ENV=development` relaxes the production checks, and `GIT_SMS_SENDER=console` logs replies instead of
sending them (`GIT_SMS_DEV_OUTBOX=outbox.jsonl` also records them). [docs/TESTING.md](docs/TESTING.md) describes the
live end-to-end harness.
