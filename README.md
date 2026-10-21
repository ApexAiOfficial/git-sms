# Git SMS 📲

Idea: Enabling GitHub collaboration, automation, and AI interaction via SMS text messages.

> **Status:** pre-1.0 community fork of [github/git-sms](https://github.com/github/git-sms). Not a GitHub product.
> Provided as-is under the MIT license. Read [SECURITY.md](SECURITY.md) before you deploy it.

Text a phone number, work on GitHub:

```text
you > use acme/field-reports
git > Default repo is now acme/field-reports. Try ISSUES or ISSUE #1.
you > new issue Pump 3 leaks -- Water on the floor near pump 3 since 6am
git > Reply CONFIRM 4821 within 10 min to open an issue in acme/field-reports: "Pump 3 leaks". ABORT drops it.
you > confirm 4821
git > Created acme/field-reports#57: https://github.com/acme/field-reports/issues/57
```

## What works today

| Command | What it does |
| --- | --- |
| `COMMANDS`, `COMMANDS <name>` | List commands, or explain one |
| `USE owner/repo` | Set a default repository, so `#12` works on its own |
| `REPO [owner/repo]` | Description, stars, open work, language, link |
| `ISSUES [owner/repo] [open\|closed\|all]` | Recent issues (pull requests excluded) |
| `ISSUE owner/repo#12` | One issue: state, author, labels, excerpt, link |
| `PRS [owner/repo]`, `PR owner/repo#5` | Open pull requests; one pull request with branch, size and mergeability |
| `SEARCH words` | Find repositories you may read |
| `SUMMARIZE owner/repo`, `SUMMARIZE owner/repo#12` | AI summary of a README or a whole issue/PR thread (optional) |
| `NEW ISSUE [owner/repo] Title -- details` | Open an issue (asks for confirmation) |
| `COMMENT owner/repo#12 text` | Comment on an issue or pull request (asks for confirmation) |
| `CLOSE` / `REOPEN owner/repo#12` | Change an issue's state (asks for confirmation; pull requests stay on GitHub) |
| `CONFIRM 1234`, `ABORT` | Approve or drop the waiting change |
| `MORE`, `WHOAMI` | Next page of a long reply; your alias and access |

Free-form messages ("what's broken in field reports?") are optionally rewritten into one of these commands by a
language model, and the reply shows how it was understood. A model can never run anything outside this grammar,
and anything that changes GitHub always needs the confirmation code.

Command words avoid the carrier keywords (`HELP`, `INFO`, `STOP`, `CANCEL`, `END`, `QUIT`, `YES`, `START`...).
With Twilio's default opt-out handling, `HELP` and `INFO` are answered by Twilio and never forwarded, so the
original `help` command could not reach the prototype; `COMMANDS` replaces it. With Advanced Opt-Out, Twilio
forwards those keywords flagged with `OptOutType`: Git SMS records the opt-out state and stays silent, so nobody
gets two replies.

## How it stays safe

Every message passes, in order: a valid **Twilio signature** for the exact public URL; a **durable, one-time
intake** (the message is committed to the queue before Twilio is answered, and redelivery is a no-op);
**opt-out** state; an operator-owned **sender directory** (unknown numbers get silence); per-sender and global
**rate and spend limits**; explicit **repository grants** (search results included); and, for any write, a
**confirmation code** that only the registered handset receives, which production cannot switch off. That last
step also defeats a spoofed sender number.

Writes are **journalled and marked**, so a crash or a redelivered message can never repeat one, and a write
GitHub did not confirm is reported as unconfirmed and never retried blindly. They run under a least-privilege
**GitHub App** installation token (or a fine-grained token) and are attributed to the sender's alias, not their
number. Queued messages, replies and pending changes are **encrypted at rest** and expire; senders are stored as
keyed hashes; every decision is audited. Details: [SECURITY.md](SECURITY.md) and
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Getting started

See [SETUP.md](SETUP.md): about 15 minutes with a Twilio number, a GitHub App (or token) and any host that can
serve HTTPS. A [Dockerfile](Dockerfile) is included.

## Status and roadmap

**Status: production foundation, not yet production-validated.** Everything below is tested against a real
GitHub repository with simulated (correctly signed) Twilio traffic; no carrier-delivered SMS has been tested yet.

- **Done (0.2):** authenticated webhook edge with a durable queue and crash recovery, sender directory and grants,
  confirmed issue/comment/close/reopen with a write journal, deterministic read commands, SMS-aware paging,
  delivery-status tracking, retention, optional AI summaries and natural-language routing under a daily budget,
  an audit log, 283 automated tests, and live end-to-end and crash-recovery harnesses against a real repository.
- **Next:** carrier-delivered verification on a registered sender (toll-free verification or A2P 10DLC), a
  GitHub App setup helper, an audit export command, optional per-user PINs.
- **Then:** allowlisted `workflow_dispatch` (`RUN deploy staging`) with run tracking, SMS notifications for
  watched issues, and an optional Android SMS-gateway transport for deployments without an SMS provider.

The full plan and its reasoning are in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md#roadmap).

## What It Is

Git SMS is an open source project that shows a proof of concept to enable users to interact with GitHub and AI via SMS. Simply by texting a number you could summarize issues, pull requests, repos, receive updates, and more.

This workflow brings the power of open source and AI to people who may not have consistent access to laptops, smartphones, or high-speed internet.

## Why It Matters

93% of the world has access to SMS.

Fewer than 65% have reliable internet or a smartphone.

Everyone should be able to participate in global innovation, contribute to the open source community, learn from others, and interact with AI.

This system enables:

- Participation in open source and AI regardless of location or hardware
- Communication over the SMS network
- Interaction with GitHub through a plain-text interface

## Use Cases

This project can be adapted for a wide variety of scenarios, from individual developer productivity to large-scale community initiatives.

### Bridging the Digital Divide & Empowering Communities

- **Offline First Contribution:** Allow individuals in areas with limited, expensive, or unreliable internet to contribute to open source projects by opening issues, commenting, and receiving updates via SMS.
- **Bug Reporting from the Field:** Enable users of open source software in remote locations to report bugs and provide feedback directly from their location using any mobile phone.
- **Community Polling & Governance:** Use SMS to allow community members to vote on project features, governance decisions, or new initiatives.
- **Educational Access:** Facilitate coding education without requiring a computer or broadband.
- **Citizen Journalism & Data Collection:** Empower individuals to submit reports, news tips, or structured data to a centralized repository.

### Developer & Project Manager Productivity

- **On the Go Issue Management:** Capture a bug or idea while away from a desk.
- **Emergency Code Review & Merge:** Support carefully controlled emergency workflows from a phone.
- **Quick Status Checks:** Get repository, issue, or pull-request status without opening a laptop.
- **Simple Project Scaffolding:** Start predefined project workflows with a concise SMS command.
- **Team Status Updates:** Submit lightweight status updates by text.

### DevOps & Automation

- **Triggering CI/CD Pipelines:** Initiate a build, test, or deployment process through a tightly controlled GitHub Actions workflow.
- **Infrastructure Status & Control:** Request status or invoke predefined operational workflows.
- **Incident Response & Management:** Receive alerts and submit incident updates from a phone.
- **Release Management:** Trigger an allowlisted release workflow.
- **Automated Reporting:** Receive scheduled summaries of repository activity.

### AI-Powered & Natural Language Interaction

- **Code & Project Summarization:** Get a concise summary of a repository or issue discussion.
- **Natural Language Bug Reports:** Convert plain-language reports into structured issue drafts.
- **Code Generation & Scaffolding:** Request bounded code-generation workflows.
- **Knowledge Base Queries:** Ask questions about project documentation.
- **AI-Assisted Task Management:** Turn natural-language requests into carefully constrained tasks.

### Beyond Software Development

- **Field Service & Maintenance Reporting:** Report equipment status or maintenance tasks by SMS.
- **Disaster Response & Coordination:** Submit incidents, resource requests, and updates over low-bandwidth networks.
- **Scientific Research & Citizen Science:** Submit observations or findings from the field.
- **Inventory & Asset Management:** Log status or location changes through controlled workflows.

## Demo

[Prototype Simulation](https://gh.io/git-sms-simulation)

## Original maintainers

- [Ashley Wolf](https://github.com/ashleywolf)
- [Dan Velton](https://github.com/dvelton)
