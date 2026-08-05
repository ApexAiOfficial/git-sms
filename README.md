# Git SMS 📲

Idea: Enabling GitHub collaboration, automation, and AI interaction via SMS text messages.

> **DISCLAIMER:** Proof-of-concept, insecure by design, do not use in production, not a GitHub product. **As-Is, At Your Own Risk.**

## Current implementation

The executable proof of concept supports `help`, repository summaries, latest-issue summaries, numbered-issue summaries, optional natural-language interpretation of that read-only set, and TwiML replies for a Twilio webhook.

Repository and issue creation remain intentionally disabled. The application imports and starts without credentials; AI-dependent commands return a useful configuration message until GitHub Models or OpenAI is configured.

## Security model

The current implementation does not yet provide Twilio signature verification, sender authorization, replay protection, idempotency, rate limiting, durable auditing, GitHub App authentication, or SMS-triggered Actions. These controls must exist before enabling writes or workflow dispatch.

## Roadmap

1. Authenticate and constrain inbound SMS requests.
2. Add idempotency, rate limits, and audit records.
3. Introduce a least-privilege GitHub App.
4. Demonstrate one allowlisted `workflow_dispatch` command with run tracking.

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

## Setup

[Step-by-step Proof of Concept](SETUP.md)

## Maintainers

- [Ashley Wolf](https://github.com/ashleywolf)
- [Dan Velton](https://github.com/dvelton)
