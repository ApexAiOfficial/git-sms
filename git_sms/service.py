"""Turn one authenticated inbound SMS into GitHub calls and reply text.

Order of checks for every message: opt-out keywords, opt-out state, sender directory,
per-sender rate limit, parsing (optionally via the LLM), repository grant, and for
writes a one-time confirmation code that only the real handset receives. Every
decision lands in the audit log; phone numbers never leave this process.
"""

from __future__ import annotations

import hmac
import logging
import secrets
from collections.abc import Callable
from dataclasses import dataclass, replace

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
from git_sms.store import Store

log = logging.getLogger(__name__)

COMMANDS_TEXT = (
    "Git SMS commands: REPO, ISSUES, ISSUE #12, PRS, PR #5, SEARCH, SUMMARIZE, "
    "NEW ISSUE, COMMENT, CLOSE, REOPEN, USE owner/repo, WHOAMI, MORE. "
    "Text COMMANDS <name> for details."
)
NOT_UNDERSTOOD = "Not understood. Text COMMANDS for the list."
MAX_CONFIRM_ATTEMPTS = 3
EXCERPT = 280


class Refusal(Exception):
    """A request that is refused with a message for the sender."""


@dataclass(frozen=True)
class Inbound:
    message_sid: str
    sender: str
    recipient: str
    body: str
    num_media: int = 0


def _excerpt(text: str | None, limit: int = EXCERPT) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 3].rstrip() + "..."


def _label(ref: Ref) -> str:
    return f"{ref.repo}#{ref.number}"


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
        command = parse(inbound.body)

        if isinstance(command, Keyword):
            if command.kind in {"opt_out", "opt_in"}:
                self.store.set_opted_out(key, command.kind == "opt_out")
                self.store.audit(sender_key=key, message_sid=inbound.message_sid, action=command.kind, outcome="ok")
                return []
            command = Help()
        if self.store.is_opted_out(key):
            return []

        user = self.directory.lookup(inbound.sender)
        if user is None:
            self.store.audit(
                sender_key=key, message_sid=inbound.message_sid, action="unknown_sender", outcome="ignored"
            )
            return []

        count = self.store.consume(f"commands:{key}", 3600)
        if count > self.settings.max_commands_per_hour:
            self.store.audit(
                sender_key=key,
                message_sid=inbound.message_sid,
                alias=user.alias,
                action="rate_limit",
                outcome="refused",
            )
            if count == self.settings.max_commands_per_hour + 1:
                return ["Message limit reached for this hour. Try again later."]
            return []

        if isinstance(command, More):
            page = self.store.next_page(key)
            return [page or "Nothing more to show."]

        interpreted = None
        if isinstance(command, Unknown):
            command, interpreted = self._interpret(command)

        audit = {"sender_key": key, "message_sid": inbound.message_sid, "alias": user.alias}
        try:
            text = self._dispatch(command, user, key, interpreted, audit)
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
        self.store.set_pages(key, pages[1:])
        return pages[:1]

    def _interpret(self, command: Unknown) -> tuple[Command, str | None]:
        if not (self.llm and self.settings.natural_language and command.text):
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

    def _dispatch(self, command: Command, user: User, key: str, interpreted: str | None, audit: dict) -> str:
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
                return self._confirm(code, user, key, audit)
            case Use(repo):
                self._authorize_read(user, repo)
                data = self._client(user).repo(repo)
                self.store.set_default_repo(key, data.get("full_name") or repo)
                self.store.audit(**audit, action="Use", repo=repo, outcome="ok")
                return f"Default repo is now {data.get('full_name') or repo}. Try ISSUES or ISSUE #1."

        if command.is_write:
            return self._request_write(command, user, key, interpreted is not None, audit)

        repo = self._repo_for(command, key)
        self._authorize_read(user, repo)
        client = self._client(user)
        text = self._read(command, client, repo)
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

    def _read(self, command: Command, client: GitHub, repo: str | None) -> str:
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
                items = client.search_repositories(query)
                if not items:
                    return "No repositories match."
                return "\n".join(
                    f"{n}) {item['full_name']} - {_excerpt(item.get('description') or 'no description', 80)}"
                    for n, item in enumerate(items, start=1)
                )
            case Summarize():
                return self._summarize(command, client, repo)
        raise Refusal(NOT_UNDERSTOOD)

    def _summarize(self, command: Summarize, client: GitHub, repo: str) -> str:
        if not self.llm:
            raise Refusal("AI summaries are not set up here. Try REPO or ISSUE instead.")
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

    def _describe(self, command: Command, user: User) -> str:
        if isinstance(command, CreateIssue):
            return f'open an issue in {command.repo}: "{_excerpt(command.title, 80)}"'
        issue = self.github.issue(command.ref.repo, command.ref.number)
        kind = "PR" if "pull_request" in issue else "issue"
        title = _excerpt(issue.get("title"), 60)
        if isinstance(command, AddComment):
            return f'comment on {kind} {_label(command.ref)} "{title}": "{_excerpt(command.body, 80)}"'
        if command.state == issue.get("state"):
            raise Refusal(f"{_label(command.ref)} is already {command.state}.")
        verb = "close" if command.state == "closed" else "reopen"
        return f'{verb} {kind} {_label(command.ref)} "{title}"'

    def _request_write(self, command: Command, user: User, key: str, interpreted: bool, audit: dict) -> str:
        command = self._resolve(command, key)
        repo = command.repo if isinstance(command, CreateIssue) else command.ref.repo
        self._authorize_write(user, repo)
        if self.store.peek(f"writes:{key}", 86400) >= self.settings.max_writes_per_day:
            raise Refusal("Daily change limit reached. Try again tomorrow.")
        description = self._describe(command, user)
        action = type(command).__name__

        if not (self.settings.confirm_writes or interpreted):
            return self._execute(command, user, key, audit)

        replaced = self.store.get_pending(key) is not None
        code = self._new_code()
        self.store.set_pending(key, code, to_record(command), self.settings.confirmation_ttl_seconds)
        self.store.audit(**audit, action=action, repo=repo, outcome="awaiting_confirmation")
        minutes = self.settings.confirmation_ttl_seconds // 60
        note = " (replaces your earlier request)" if replaced else ""
        return f"Reply CONFIRM {code} within {minutes} min to {description}{note}. ABORT drops it."

    def _confirm(self, code: str, user: User, key: str, audit: dict) -> str:
        pending = self.store.get_pending(key)
        if pending is None:
            return "Nothing is waiting for confirmation."
        if pending.expires_at < self.store.now():
            self.store.drop_pending(key)
            self.store.audit(**audit, action="Confirm", outcome="expired")
            return "That request expired. Send it again."
        if not hmac.compare_digest(pending.code, code):
            attempts = self.store.fail_pending(key)
            self.store.audit(**audit, action="Confirm", outcome="wrong_code")
            if attempts >= MAX_CONFIRM_ATTEMPTS:
                self.store.drop_pending(key)
                return "Wrong code too many times. The request was dropped."
            return "Wrong code. Check the latest code and try again."
        taken = self.store.take_pending(key)
        if taken is None or taken.code != pending.code:
            return "Nothing is waiting for confirmation."
        command = from_record(taken.command)
        repo = command.repo if isinstance(command, CreateIssue) else command.ref.repo
        self._authorize_write(user, repo)
        return self._execute(command, user, key, audit)

    def _execute(self, command: Command, user: User, key: str, audit: dict) -> str:
        if self.store.consume(f"writes:{key}", 86400) > self.settings.max_writes_per_day:
            raise Refusal("Daily change limit reached. Try again tomorrow.")
        footer = f"\n\n<sub>Sent by {user.alias} via Git SMS</sub>"
        action = type(command).__name__
        match command:
            case CreateIssue(repo, title, body):
                issue = self.github.create_issue(repo, title, (body + footer).strip())
                self.store.audit(**audit, action=action, repo=repo, outcome="done", url=issue["html_url"])
                return f"Created {repo}#{issue['number']}: {issue['html_url']}"
            case AddComment(ref, body):
                comment = self.github.add_comment(ref.repo, ref.number, body + footer)
                self.store.audit(**audit, action=action, repo=ref.repo, outcome="done", url=comment["html_url"])
                return f"Commented on {_label(ref)}: {comment['html_url']}"
            case SetState(ref, state):
                issue = self.github.set_state(ref.repo, ref.number, state)
                self.store.audit(**audit, action=action, repo=ref.repo, outcome="done", url=issue["html_url"])
                return f"{'Closed' if state == 'closed' else 'Reopened'} {_label(ref)}."
        raise Refusal(NOT_UNDERSTOOD)
