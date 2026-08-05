## Setup Instructions

Git SMS is a read-only proof of concept for summarizing public GitHub repositories and issues through an SMS webhook. Write operations remain intentionally disabled.

### Install

```bash
git clone https://github.com/your-username/git-sms.git
cd git-sms
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

Tested on Python 3.10, 3.11, and 3.12.

### Optional credentials

Create `.env` when you want AI summaries:

```env
GITHUB_OPENAI_API_KEY=your_github_models_token
# OPENAI_API_KEY=your_openai_api_key
# GITHUB_TOKEN=your_optional_read_only_token
```

The application imports and starts without credentials. AI-dependent commands return a clear SMS response until a provider key is configured.

### Run

```bash
uvicorn main:app --reload
```

Expose port 8000 and configure a Twilio incoming-message webhook to POST to `/webhook`.

> This proof of concept does not validate Twilio signatures or authorize senders. Do not deploy it in production.

### Commands

```text
help
summarize github/git-sms
summarize owner/repo issue
summarize owner/repo issue 123
```
