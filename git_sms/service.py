"""Turn one authenticated inbound SMS into GitHub calls and reply text.

Order of checks for every message: Twilio opt-out signals and keywords, opt-out state,
sender directory, per-sender rate limit, parsing (optionally via the LLM, within a
global daily budget), repository grant, and for writes a one-time confirmation code
that only the real handset receives. Every decision lands in the audit log.

Writes are journalled before they are attempted, keyed to the MessageSid that
triggered them, so a re-processed message can never repeat a write. A write whose
outcome GitHub did not confirm (timeout, dropped connection, 5xx) is recorded as
unknown and never retried automatically; it is reconciled by looking for the hidden
marker the write carries.
"""

from __future__ import annotations

import logging
import secrets
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime

from git_sms.commands import (
    USAGE,
    Abort,
    AddComment,
    Command,
    Confirm,
    CreateIssue,
    Help,
    Invalid,
    Keyword,
    ListIssues,
    ListPulls,
    More,
    Ref,
    RepoInfo,
    Search,
    SetState,
    ShowIssue,
    ShowPull,
    Summarize,
    Unknown,
    Use,
    WhoAmI,
    canonical,
    from_record,
    parse,
    to_record,
)
from git_sms.config import Settings
from git_sms.github import GitHub, GitHubError
from git_sms.identity import Directory, User
from git_sms.llm import LLM, LLMError
from git_sms.sms import paginate
from git_sms.store import Operation, Store

log = logging.getLogger(__name__)

COMMANDS_TEXT = (
    "Git SMS commands: REPO, ISSUES, ISSUE #12, PRS, PR #5, SEARCH, SUMMARIZE, "
    "NEW ISSUE, COMMENT, CLOSE, REOPEN, USE owner/repo, WHOAMI, MORE. "
    "Text COMMANDS <name> for details."
)
NOT_UNDERSTOOD = "Not understood. Text COMMANDS for the list."
AI_EXHAUSTED = "AI features have reached today's limit. Try REPO or ISSUE instead."
MAX_CONFIRM_ATTEMPTS = 3
EXCERPT = 280
PAGES_TTL_SECONDS = 3600
DUPLICATE_WINDOW_SECONDS = 86400
RECONCILE_SLACK_SECONDS = 300
OPT_OUT_TYPES = {"STOP": "opt_out", "START": "opt_in", "HELP": "help"}


class Refusal(Exception):
    """A request that is refused with a message for the sender."""


@dataclass(frozen=True)
class Inbound:
    message_sid: str
    sender: str
    recipient: str
    body: str
    num_media: int = 0
    opt_out_type: str | None = None  # Twilio's OptOutType (Advanced Opt-Out), when present


def _excerpt(text: str | None, limit: int = EXCERPT) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 3].rstrip() + "..."


def _label(ref: Ref) -> str:
    return f"{ref.repo}#{ref.number}"


def _iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _target(command: Command) -> tuple[str, int | None]:
    if isinstance(command, CreateIssue):
        return command.repo, None
    return command.ref.repo, command.ref.number


class Service:
    def __init__(
        self,
        settings: Settings,
        store: Store,
        directory: Directory,
        github: GitHub,
        guest_github: GitHub | None = None,
        llm: LLM | None = None,
        code_source: Callable[[], str] = lambda: f"{secrets.randbelow(10_000):04d}",
    ):
        self.settings = settings
        self.store = store
        self.directory = directory
        self.github = github
        self.guest_github = guest_github or github
        self.llm = llm
        self._new_code = code_source

    # Entry point ---------------------------------------------------------------------

    def handle(self, inbound: Inbound) -> list[str]:
        """Return the replies to send (usually one); an empty list means stay silent."""
        key = self.store.sender_key(inbound.sender)
        audit_base = {"sender_key": key, "message_sid": inbound.message_sid}

        twilio_kind = OPT_OUT_TYPES.get((inbound.opt_out_type or "").upper())
        if twilio_kind:
            # Advanced Opt-Out: Twilio has already acted and auto-replied; record, never answer twice.
            if twilio_kind != "help":
                self.store.set_opted_out(key, twilio_kind == "opt_out")
            self.store.audit(**audit_base, action=f"twilio_{twilio_kind}", outcome="ok")
            return []

        command = parse(inbound.body)
        if isinstance(command, Keyword):
            if command.kind in {"opt_out", "opt_in"}:
                self.store.set_opted_out(key, command.kind == "opt_out")
                self.store.audit(**audit_base, action=command.kind, outcome="ok")
                return []
            command = Help()
        if self.store.is_opted_out(key):
            return []

        user = self.directory.lookup(inbound.sender)
        if user is None:
            self.store.audit(**audit_base, action="unknown_sender", outcome="ignored")
            return []

        count = self.store.consume(f"commands:{key}", 3600)
        if count > self.settings.max_commands_per_hour:
            self.store.audit(**audit_base, alias=user.alias, action="rate_limit", outcome="refused")
            if count == self.settings.max_commands_per_hour + 1:
                return ["Message limit reached for this hour. Try again later."]
            return []

        if isinstance(command, More):
            page = self.store.next_page(key)
            return [page or "Nothing more to show."]

        interpreted = None
        if isinstance(command, Unknown):
            command, interpreted = self._interpret(command, user)

        audit = {**audit_base, "alias": user.alias}
        op_id = self.store.op_id(inbound.message_sid)
        try:
            text = self._dispatch(command, user, key, interpreted, audit, op_id)
        except Refusal as refusal:
            text = str(refusal)
            self.store.audit(**audit, action=type(command).__name__, outcome="refused", detail=text)
        except GitHubError as error:
            text = str(error)
            self.store.audit(**audit, action=type(command).__name__, outcome="github_error", detail=text)
        except LLMError as error:
            log.warning("LLM failure: %s", error)
            text = "AI summaries are unavailable right now. Try again later."
            self.store.audit(**audit, action=type(command).__name__, outcome="llm_error", detail=str(error))
        except Exception:
            log.exception("Unexpected failure handling %s", type(command).__name__)
            text = "Something went wrong. Try again later."
            self.store.audit(**audit, action=type(command).__name__, outcome="error")

        if interpreted and not command.is_write:
            text = f"({interpreted})\n{text}"
        pages = paginate(text, self.settings.max_segments_per_reply)
        self.store.set_pages(key, pages[1:], PAGES_TTL_SECONDS)
        return pages[:1]

    def _ai_allowed(self, user: User) -> bool:
        """Guests never use the model; everyone else shares one global daily call budget."""
        if user.guest or not self.llm:
            return False
        return self.store.consume("ai_calls:all", 86400) <= self.settings.max_ai_calls_per_day

    def _interpret(self, command: Unknown, user: User) -> tuple[Command, str | None]:
        if not (self.llm and self.settings.natural_language and command.text) or not self._ai_allowed(user):
            return command, None
        try:
            line = self.llm.interpret(command.text)
        except LLMError as error:
            log.warning("LLM interpretation failed: %s", error)
            return command, None
        if not line:
            return command, None
        rewritten = parse(line)
        if isinstance(rewritten, Unknown | Invalid | Keyword | Confirm | Abort | More):
            return command, None
        return rewritten, canonical(rewritten)

    # Dispatch ------------------------------------------------------------------------

    def _dispatch(
        self, command: Command, user: User, key: str, interpreted: str | None, audit: dict, op_id: str
    ) -> str:
        match command:
            case Unknown():
                return NOT_UNDERSTOOD
            case Invalid(usage):
                return f"Usage: {usage}"
            case Help(topic):
                return USAGE.get(topic, COMMANDS_TEXT) if topic else COMMANDS_TEXT
            case WhoAmI():
                return self._whoami(user, key)
            case Abort():
                return (
                    "Dropped the change waiting for confirmation."
                    if self.store.drop_pending(key)
                    else ("Nothing is waiting for confirmation.")
                )
            case Confirm(code):
                return self._confirm(code, user, key, audit, op_id)
            case Use(repo):
                self._authorize_read(user, repo)
                data = self._client(user).repo(repo)
                self.store.set_default_repo(key, data.get("full_name") or repo)
                self.store.audit(**audit, action="Use", repo=repo, outcome="ok")
                return f"Default repo is now {data.get('full_name') or repo}. Try ISSUES or ISSUE #1."

        if command.is_write:
            return self._request_write(command, user, key, interpreted is not None, audit, op_id)

        repo = self._repo_for(command, key)
        self._authorize_read(user, repo)
        client = self._client(user)
        text = self._read(command, client, repo, user)
        self.store.audit(**audit, action=type(command).__name__, repo=repo, outcome="ok")
        return text

    def _repo_for(self, command: Command, key: str) -> str | None:
        if isinstance(command, Search):
            return None
        explicit = getattr(command, "repo", None)
        ref = getattr(command, "ref", None)
        repo = explicit or (ref.repo if ref else None) or self.store.default_repo(key)
        if not repo:
            raise Refusal("Which repo? Add owner/repo, or text USE owner/repo first.")
        return repo

    def _authorize_read(self, user: User, repo: str | None) -> None:
        if repo is not None and not user.can_read(repo):
            raise Refusal(f"You don't have access to {repo}.")

    def _client(self, user: User) -> GitHub:
        return self.guest_github if user.guest else self.github

    def _whoami(self, user: User, key: str) -> str:
        default = self.store.default_repo(key) or "none"
        if user.guest:
            return f"You are a guest: read-only access to public repositories. Default repo: {default}."
        read = ", ".join(user.read) or "write repos only"
        write = ", ".join(user.write) or "none"
        return f"You are {user.alias}. Read: {read}. Write: {write}. Default repo: {default}."

    # Reads ---------------------------------------------------------------------------

    def _read(self, command: Command, client: GitHub, repo: str | None, user: User) -> str:
        match command:
            case RepoInfo():
                data = client.repo(repo)
                lines = [f"{data['full_name']}{' (private)' if data.get('private') else ''}"]
                if data.get("description"):
                    lines.append(_excerpt(data["description"], 160))
                lines.append(
                    f"Stars {data.get('stargazers_count', 0)}, forks {data.get('forks_count', 0)}, "
                    f"open issues+PRs {data.get('open_issues_count', 0)}"
                )
                lines.append(f"{data.get('language') or 'No language'}, default branch {data.get('default_branch')}")
                if data.get("archived"):
                    lines.append("Archived (read-only).")
                lines.append(data.get("html_url", ""))
                return "\n".join(line for line in lines if line)
            case ListIssues(_, state):
                issues = client.issues(repo, state)
                if not issues:
                    return f"No {state if state != 'all' else ''} issues in {repo}.".replace("  ", " ")
                lines = [f"{repo} {state} issues:"]
                lines += [
                    f"#{i['number']} {_excerpt(i['title'], 70)}"
                    + (f" ({i['comments']}c)" if i.get("comments") else "")
                    + (" [closed]" if state == "all" and i.get("state") == "closed" else "")
                    for i in issues
                ]
                return "\n".join(lines)
            case ShowIssue(ref):
                issue = client.issue(repo, ref.number)
                if "pull_request" in issue:
                    return f"#{ref.number} is a pull request. Text PR {repo}#{ref.number}."
                author = (issue.get("user") or {}).get("login", "unknown")
                labels = ", ".join(label["name"] for label in issue.get("labels", []) if isinstance(label, dict))
                lines = [
                    f"{repo}#{ref.number} [{issue['state']}] {_excerpt(issue['title'], 120)}",
                    f"by {author}, {issue.get('comments', 0)} comments" + (f", labels: {labels}" if labels else ""),
                ]
                if issue.get("body"):
                    lines.append(_excerpt(issue["body"]))
                lines.append(issue["html_url"])
                return "\n".join(lines)
            case ListPulls():
                pulls = client.pulls(repo)
                if not pulls:
                    return f"No open pull requests in {repo}."
                lines = [f"{repo} open PRs:"]
                lines += [
                    f"#{p['number']} {_excerpt(p['title'], 70)} by {(p.get('user') or {}).get('login', '?')}"
                    + (" [draft]" if p.get("draft") else "")
                    for p in pulls
                ]
                return "\n".join(lines)
            case ShowPull(ref):
                pull = client.pull(repo, ref.number)
                state = "merged" if pull.get("merged") else pull["state"]
                mergeable = {True: "yes", False: "no"}.get(pull.get("mergeable"), "unknown")
                lines = [
                    f"{repo} PR #{ref.number} [{state}{', draft' if pull.get('draft') else ''}] "
                    f"{_excerpt(pull['title'], 120)}",
                    f"by {(pull.get('user') or {}).get('login', 'unknown')}: "
                    f"{(pull.get('head') or {}).get('ref', '?')} -> {(pull.get('base') or {}).get('ref', '?')}",
                    f"+{pull.get('additions', 0)} -{pull.get('deletions', 0)} in {pull.get('changed_files', 0)} files, "
                    f"mergeable: {mergeable}",
                    pull["html_url"],
                ]
                return "\n".join(lines)
            case Search(query):
                # The service credential may see private repositories outside this sender's
                # grants: filter before anything is formatted.
                items = [
                    item
                    for item in client.search_repositories(query)
                    if isinstance(item, dict) and user.can_read(str(item.get("full_name", "")))
                ][:3]
                if not items:
                    return "No repositories match."
                return "\n".join(
                    f"{n}) {item['full_name']} - {_excerpt(item.get('description') or 'no description', 80)}"
                    for n, item in enumerate(items, start=1)
                )
            case Summarize():
                return self._summarize(command, client, repo, user)
        raise Refusal(NOT_UNDERSTOOD)

    def _summarize(self, command: Summarize, client: GitHub, repo: str, user: User) -> str:
        if not self.llm:
            raise Refusal("AI summaries are not set up here. Try REPO or ISSUE instead.")
        if user.guest:
            raise Refusal("AI summaries are for registered users. Try REPO or ISSUE instead.")
        if not self._ai_allowed(user):
            raise Refusal(AI_EXHAUSTED)
        number = command.number
        if command.latest_issue:
            latest = client.issues(repo, "open", limit=1)
            if not latest:
                return f"No open issues in {repo}."
            number = latest[0]["number"]
        if number is None:
            data = client.repo(repo)
            try:
                readme = client.readme(repo)
            except GitHubError as error:
                if error.status != 404:
                    raise
                readme = "(no README)"
            content = (
                f"Repository {data['full_name']}\nDescription: {data.get('description') or 'none'}\n"
                f"Language: {data.get('language') or 'unknown'}\nStars: {data.get('stargazers_count', 0)}\n\n"
                f"README:\n{readme}"
            )
            return f"{data['full_name']}: {self.llm.summarize('GitHub repository', content)}"
        issue = client.issue(repo, number)
        kind = "pull request" if "pull_request" in issue else "issue"
        comments = client.comments(repo, number)
        thread = [f"{kind.title()} #{number}: {issue['title']} [{issue['state']}]", issue.get("body") or ""]
        thread += [f"{(c.get('user') or {}).get('login', '?')}: {c.get('body') or ''}" for c in comments]
        return f"{repo}#{number}: {self.llm.summarize('GitHub ' + kind + ' thread', chr(10).join(thread))}"

    # Writes --------------------------------------------------------------------------

    def _resolve(self, command: Command, key: str) -> Command:
        repo = self._repo_for(command, key)
        if isinstance(command, CreateIssue):
            return replace(command, repo=repo)
        return replace(command, ref=Ref(repo, command.ref.number))

    def _authorize_write(self, user: User, repo: str) -> None:
        if user.guest:
            raise Refusal("Guests can only read. Ask the operator for write access.")
        if not user.can_write(repo):
            raise Refusal(f"You can't change {repo}.")

    def _describe(self, command: Command) -> str:
        if isinstance(command, CreateIssue):
            return f'open an issue in {command.repo}: "{_excerpt(command.title, 80)}"'
        issue = self.github.issue(command.ref.repo, command.ref.number)
        is_pull = "pull_request" in issue
        title = _excerpt(issue.get("title"), 60)
        if isinstance(command, AddComment):
            kind = "PR" if is_pull else "issue"
            return f'comment on {kind} {_label(command.ref)} "{title}": "{_excerpt(command.body, 80)}"'
        if is_pull:
            # Closing a PR needs the Pull requests permission and is a code-review decision; keep it on GitHub.
            raise Refusal("CLOSE and REOPEN work on issues only. Manage pull requests on GitHub.")
        if command.state == issue.get("state"):
            raise Refusal(f"{_label(command.ref)} is already {command.state}.")
        verb = "close" if command.state == "closed" else "reopen"
        return f'{verb} issue {_label(command.ref)} "{title}"'

    def _fingerprint(self, command: Command) -> str:
        return self.store.fingerprint(canonical(command))

    def _duplicate_note(self, command: Command, key: str) -> str:
        earlier = self.store.similar_operation(key, self._fingerprint(command), DUPLICATE_WINDOW_SECONDS)
        if earlier is None:
            return ""
        when = datetime.fromtimestamp(earlier.created_at, UTC).strftime("%H:%M UTC")
        if earlier.status == "done":
            return f" Note: you already made this change at {when}: {earlier.url}."
        return f" Note: an identical change at {when} has an unconfirmed outcome; check {self._link(earlier)} first."

    def _request_write(self, command: Command, user: User, key: str, interpreted: bool, audit: dict, op_id: str) -> str:
        command = self._resolve(command, key)
        repo, _ = _target(command)
        self._authorize_write(user, repo)
        if self.store.peek(f"writes:{key}", 86400) >= self.settings.max_writes_per_day:
            raise Refusal("Daily change limit reached. Try again tomorrow.")
        description = self._describe(command)
        action = type(command).__name__

        if not (self.settings.confirm_writes or interpreted):
            return self._execute(command, user, key, audit, op_id)

        code = self._new_code()
        replaced = self.store.set_pending(key, code, to_record(command), self.settings.confirmation_ttl_seconds)
        self.store.audit(**audit, action=action, repo=repo, outcome="awaiting_confirmation")
        minutes = self.settings.confirmation_ttl_seconds // 60
        note = " (replaces your earlier request)" if replaced else ""
        return (
            f"Reply CONFIRM {code} within {minutes} min to {description}{note}. ABORT drops it."
            + self._duplicate_note(command, key)
        )

    def _confirm(self, code: str, user: User, key: str, audit: dict, op_id: str) -> str:
        existing = self.store.operation(op_id)
        if existing is not None:
            # This very CONFIRM was already acted on (we are re-processing it after a restart).
            return self._settle(existing)
        outcome, record = self.store.confirm_pending(key, code, MAX_CONFIRM_ATTEMPTS)
        if outcome == "none":
            return "Nothing is waiting for confirmation."
        if outcome == "expired":
            self.store.audit(**audit, action="Confirm", outcome="expired")
            return "That request expired. Send it again."
        if outcome in {"wrong", "locked"}:
            self.store.audit(**audit, action="Confirm", outcome="wrong_code")
            if outcome == "locked":
                return "Wrong code too many times. The request was dropped."
            return "Wrong code. Check the latest code and try again."
        command = from_record(record)
        self._authorize_write(user, _target(command)[0])
        return self._execute(command, user, key, audit, op_id)

    def _execute(self, command: Command, user: User, key: str, audit: dict, op_id: str) -> str:
        repo, number = _target(command)
        action = type(command).__name__ + (f":{command.state}" if isinstance(command, SetState) else "")
        earlier = self.store.begin_operation(op_id, key, action, repo, number, self._fingerprint(command))
        if earlier is not None:
            return self._settle(earlier)
        if self.store.consume(f"writes:{key}", 86400) > self.settings.max_writes_per_day:
            self.store.finish_operation(op_id, "failed")
            raise Refusal("Daily change limit reached. Try again tomorrow.")

        tail = f"\n\n<sub>Sent by {user.alias} via Git SMS</sub>\n<!-- git-sms:{op_id} -->"
        try:
            match command:
                case CreateIssue(_, title, body):
                    issue = self.github.create_issue(repo, title, (body + tail).strip())
                    url, reply = issue["html_url"], f"Created {repo}#{issue['number']}: {issue['html_url']}"
                case AddComment(ref, body):
                    comment = self.github.add_comment(repo, number, body + tail)
                    url, reply = comment["html_url"], f"Commented on {_label(ref)}: {comment['html_url']}"
                case SetState(ref, state):
                    issue = self.github.set_state(repo, number, state)
                    url, reply = issue["html_url"], f"{'Closed' if state == 'closed' else 'Reopened'} {_label(ref)}."
                case _:
                    raise Refusal(NOT_UNDERSTOOD)
        except GitHubError as error:
            if not error.ambiguous:
                self.store.finish_operation(op_id, "failed")
                raise
            self.store.finish_operation(op_id, "unknown")
            self.store.audit(**audit, action=action, repo=repo, outcome="unknown", detail=str(error))
            return self._unknown_reply(self.store.operation(op_id))
        self.store.finish_operation(op_id, "done", url)
        self.store.audit(**audit, action=action, repo=repo, outcome="done", url=url)
        return reply

    # Unknown outcomes ----------------------------------------------------------------

    def _link(self, op: Operation) -> str:
        return self.github.web_url(f"{op.repo}/issues" + (f"/{op.number}" if op.number else ""))

    def _unknown_reply(self, op: Operation) -> str:
        return f"GitHub did not confirm whether that change was made. Check {self._link(op)} before sending it again."

    def _settle(self, op: Operation) -> str:
        """Report on a write that already ran, reconciling it with GitHub if its outcome is open."""
        if op.status == "done":
            return f"Already done: {op.url}"
        if op.status == "failed":
            return "That change was not made."
        url = self._reconcile(op)
        if url:
            self.store.finish_operation(op.op_id, "done", url)
            return f"Done: {url}"
        self.store.finish_operation(op.op_id, "unknown")
        return self._unknown_reply(op)

    def _reconcile(self, op: Operation) -> str | None:
        """Find the write's hidden marker on GitHub; None means not found (which is not proof it failed)."""
        marker = f"<!-- git-sms:{op.op_id} -->"
        since = _iso(op.created_at - RECONCILE_SLACK_SECONDS)
        try:
            if op.action == "CreateIssue":
                for issue in self.github.recent_issues(op.repo, since):
                    if marker in (issue.get("body") or ""):
                        return issue["html_url"]
            elif op.action == "AddComment":
                for comment in self.github.comments(op.repo, op.number, since=since):
                    if marker in (comment.get("body") or ""):
                        return comment["html_url"]
            elif op.action.startswith("SetState:"):
                issue = self.github.issue(op.repo, op.number)
                if issue.get("state") == op.action.split(":", 1)[1]:
                    return issue["html_url"]
        except GitHubError as error:
            log.warning("Reconciliation lookup failed: %s", error)
        return None
