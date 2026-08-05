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

## Why it matters

SMS can provide a low-bandwidth interface for open-source participation, repository status, education, field reporting, and carefully bounded automation.

## Demo

[Prototype Simulation](https://gh.io/git-sms-simulation)

## Setup

[Step-by-step Proof of Concept](SETUP.md)

## Maintainers

- [Ashley Wolf](https://github.com/ashleywolf)
- [Dan Velton](https://github.com/dvelton)
