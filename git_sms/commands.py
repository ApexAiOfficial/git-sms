"""Deterministic SMS command grammar.

Every message, including one an LLM rewrote, becomes exactly one typed command here,
so nothing outside this grammar can ever execute. Command words deliberately avoid
the carrier and Twilio keywords (HELP, INFO, STOP, CANCEL, END, QUIT, YES, START...):
those are intercepted before the webhook or change the sender's opt-out state.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

OPT_OUT = frozenset({"stop", "stopall", "unsubscribe", "cancel", "end", "quit", "revoke", "optout"})
OPT_IN = frozenset({"start", "yes", "unstop"})
CARRIER_HELP = frozenset({"help", "info"})

OWNER = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})"
NAME = r"[A-Za-z0-9._-]{1,100}"
REPO_RE = re.compile(rf"^({OWNER})/({NAME})$")
REF_RE = re.compile(rf"^(?:({OWNER}/{NAME}))?#(\d{{1,9}})$")
CONFIRM_RE = re.compile(r"^\d{4}$")
MAX_TITLE = 256
MAX_TEXT = 1500


@dataclass(frozen=True)
class Ref:
    repo: str | None
    number: int


@dataclass(frozen=True)
class Command:
    """Base class; `is_write` marks commands that change GitHub."""

    is_write = False


@dataclass(frozen=True)
class Help(Command):
    topic: str | None = None


@dataclass(frozen=True)
class WhoAmI(Command):
    pass


@dataclass(frozen=True)
class Use(Command):
    repo: str


@dataclass(frozen=True)
class More(Command):
    pass


@dataclass(frozen=True)
class RepoInfo(Command):
    repo: str | None = None


@dataclass(frozen=True)
class ListIssues(Command):
    repo: str | None = None
    state: str = "open"


@dataclass(frozen=True)
class ShowIssue(Command):
    ref: Ref


@dataclass(frozen=True)
class ListPulls(Command):
    repo: str | None = None


@dataclass(frozen=True)
class ShowPull(Command):
    ref: Ref


@dataclass(frozen=True)
class Search(Command):
    query: str


@dataclass(frozen=True)
class Summarize(Command):
    repo: str | None = None
    number: int | None = None
    latest_issue: bool = False


@dataclass(frozen=True)
class CreateIssue(Command):
    repo: str | None
    title: str
    body: str = ""
    is_write = True


@dataclass(frozen=True)
class AddComment(Command):
    ref: Ref
    body: str
    is_write = True


@dataclass(frozen=True)
class SetState(Command):
    ref: Ref
    state: str  # "closed" or "open"
    is_write = True


@dataclass(frozen=True)
class Confirm(Command):
    code: str


@dataclass(frozen=True)
class Abort(Command):
    pass


@dataclass(frozen=True)
class Keyword(Command):
    kind: str  # "opt_out", "opt_in" or "help"


@dataclass(frozen=True)
class Invalid(Command):
    """Recognised command word, unusable arguments; `usage` says how to fix it."""

    usage: str


@dataclass(frozen=True)
class Unknown(Command):
    text: str


USAGE = {
    "commands": "COMMANDS - list commands. HELP <command> for details.",
    "whoami": "WHOAMI - show your alias and access.",
    "use": "USE owner/repo - set your default repo, so #12 works alone.",
    "more": "MORE - next part of a long reply.",
    "repo": "REPO [owner/repo] - repository overview.",
    "issues": "ISSUES [owner/repo] [open|closed|all] - recent issues.",
    "issue": "ISSUE owner/repo#12 (or #12) - one issue.",
    "prs": "PRS [owner/repo] - open pull requests.",
    "pr": "PR owner/repo#5 (or #5) - one pull request.",
    "search": "SEARCH words - find repositories.",
    "summarize": "SUMMARIZE owner/repo | owner/repo#12 | owner/repo ISSUE - AI summary.",
    "new": "NEW ISSUE [owner/repo] Title -- optional details",
    "comment": "COMMENT owner/repo#12 your text (or COMMENT #12 text)",
    "close": "CLOSE owner/repo#12 (or CLOSE #12)",
    "reopen": "REOPEN owner/repo#12 (or REOPEN #12)",
    "confirm": "CONFIRM 1234 - approve the change you just requested.",
    "abort": "ABORT - drop the change waiting for confirmation.",
}

ALIASES = {
    "?": "commands",
    "menu": "commands",
    "cmds": "commands",
    "next": "more",
    "repository": "repo",
    "pulls": "prs",
    "pull": "pr",
    "summary": "summarize",
    "summarise": "summarize",
    "find": "search",
    "discard": "abort",
    "nevermind": "abort",
    "create": "new",
    "open": "new",  # OPEN ISSUE ... reads naturally; it still needs the confirmation code
}


def normalize_repo(token: str) -> str | None:
    token = token.strip().removeprefix("https://github.com/").rstrip("/")
    match = REPO_RE.match(token)
    if not match or match.group(2) in {".", ".."}:
        return None
    return f"{match.group(1)}/{match.group(2)}"


def parse_ref(token: str) -> Ref | None:
    match = REF_RE.match(token.strip())
    if not match:
        return None
    repo = normalize_repo(match.group(1)) if match.group(1) else None
    if match.group(1) and not repo:
        return None
    number = int(match.group(2))
    return Ref(repo, number) if number > 0 else None


def _ref_from(args: list[str]) -> tuple[Ref | None, list[str]]:
    """Accept `owner/repo#12`, `#12`, or `owner/repo 12` at the front of args."""
    if not args:
        return None, args
    ref = parse_ref(args[0])
    if ref:
        return ref, args[1:]
    repo = normalize_repo(args[0])
    if repo and len(args) > 1 and args[1].lstrip("#").isdigit() and int(args[1].lstrip("#")) > 0:
        return Ref(repo, int(args[1].lstrip("#"))), args[2:]
    return None, args


def _split_title_body(text: str) -> tuple[str, str]:
    for separator in (" -- ", "\n"):
        if separator in text:
            title, body = text.split(separator, 1)
            return title.strip(), body.strip()
    if text.endswith(" --"):
        return text[:-3].strip(), ""
    return text.strip(), ""


def parse(text: str) -> Command:
    raw = (text or "").strip()
    if not raw:
        return Unknown("")
    words = raw.split()
    whole = raw.lower().strip(" .!")
    if whole in OPT_OUT:
        return Keyword("opt_out")
    if whole in OPT_IN:
        return Keyword("opt_in")
    if whole in CARRIER_HELP:
        return Keyword("help")

    verb = words[0].lower()
    verb = ALIASES.get(verb, verb)
    args = words[1:]
    rest = raw[len(words[0]) :].strip()

    if verb in {"commands", "help"}:
        topic = args[0].lower() if args else None
        return Help(ALIASES.get(topic, topic) if topic else None)
    if verb == "whoami":
        return WhoAmI()
    if verb == "more":
        return More()
    if verb == "abort":
        return Abort()
    if verb == "confirm":
        if len(args) == 1 and CONFIRM_RE.match(args[0]):
            return Confirm(args[0])
        return Invalid(USAGE["confirm"])
    if verb == "use":
        repo = normalize_repo(args[0]) if len(args) == 1 else None
        return Use(repo) if repo else Invalid(USAGE["use"])
    if verb == "repo":
        if not args:
            return RepoInfo()
        repo = normalize_repo(args[0]) if len(args) == 1 else None
        return RepoInfo(repo) if repo else Invalid(USAGE["repo"])
    if verb == "issues":
        repo, state = None, "open"
        for arg in args:
            if arg.lower() in {"open", "closed", "all"}:
                state = arg.lower()
            elif normalize_repo(arg) and repo is None:
                repo = normalize_repo(arg)
            else:
                return Invalid(USAGE["issues"])
        return ListIssues(repo, state)
    if verb == "prs":
        if not args:
            return ListPulls()
        repo = normalize_repo(args[0]) if len(args) == 1 else None
        return ListPulls(repo) if repo else Invalid(USAGE["prs"])
    if verb in {"issue", "pr"}:
        if verb == "issue" and args and args[0].lower() == "new":
            return _parse_new_issue(rest[3:].strip())
        ref, leftover = _ref_from(args)
        if not ref or leftover:
            return Invalid(USAGE[verb])
        return ShowIssue(ref) if verb == "issue" else ShowPull(ref)
    if verb == "search":
        return Search(rest[:200]) if rest else Invalid(USAGE["search"])
    if verb == "summarize":
        return _parse_summarize(args)
    if verb == "new":
        if args and args[0].lower() == "issue":
            return _parse_new_issue(rest[5:].strip())
        return Invalid(USAGE["new"])
    if verb == "comment":
        ref, _ = _ref_from(args)
        if not ref:
            return Invalid(USAGE["comment"])
        consumed = 1 if parse_ref(args[0]) else 2
        parts = rest.split(None, consumed)
        text = parts[consumed].strip() if len(parts) > consumed else ""
        if not text:
            return Invalid(USAGE["comment"])
        return AddComment(ref, text[:MAX_TEXT])
    if verb in {"close", "reopen"}:
        ref, leftover = _ref_from(args)
        if not ref or leftover:
            return Invalid(USAGE[verb])
        return SetState(ref, "closed" if verb == "close" else "open")
    return Unknown(raw)


def _parse_new_issue(text: str) -> Command:
    words = text.split(None, 1)
    repo = normalize_repo(words[0]) if words else None
    if repo:
        text = words[1] if len(words) > 1 else ""
    title, body = _split_title_body(text)
    if not title:
        return Invalid(USAGE["new"])
    return CreateIssue(repo, title[:MAX_TITLE], body[:MAX_TEXT])


def _parse_summarize(args: list[str]) -> Command:
    if not args:
        return Summarize()
    ref = parse_ref(args[0])
    if ref and len(args) == 1:
        return Summarize(ref.repo, ref.number)
    repo = normalize_repo(args[0])
    if not repo:
        return Invalid(USAGE["summarize"])
    if len(args) == 1:
        return Summarize(repo)
    if args[1].lower() == "issue" and len(args) == 2:
        return Summarize(repo, latest_issue=True)
    if args[1].lower() == "issue" and len(args) == 3 and args[2].lstrip("#").isdigit():
        number = int(args[2].lstrip("#"))
        return Summarize(repo, number) if number > 0 else Invalid(USAGE["summarize"])
    return Invalid(USAGE["summarize"])


WRITE_TYPES = {cls.__name__: cls for cls in (CreateIssue, AddComment, SetState)}


def to_record(command: Command) -> dict:
    """Serialise a resolved write command for the confirmation store."""
    record: dict = {"type": type(command).__name__}
    for name, value in vars(command).items():
        record[name] = {"repo": value.repo, "number": value.number} if isinstance(value, Ref) else value
    return record


def from_record(record: dict) -> Command:
    record = dict(record)
    cls = WRITE_TYPES[record.pop("type")]
    if "ref" in record:
        record["ref"] = Ref(**record["ref"])
    return cls(**record)


def canonical(command: Command) -> str:
    """Render a command back to the text a person would send, for confirmations."""

    def ref(r: Ref) -> str:
        return f"{r.repo or ''}#{r.number}"

    match command:
        case CreateIssue(repo, title, body):
            return f"NEW ISSUE {repo + ' ' if repo else ''}{title}" + (f" -- {body}" if body else "")
        case AddComment(r, body):
            return f"COMMENT {ref(r)} {body}"
        case SetState(r, state):
            return f"{'CLOSE' if state == 'closed' else 'REOPEN'} {ref(r)}"
        case RepoInfo(repo):
            return f"REPO {repo or ''}".strip()
        case ListIssues(repo, state):
            return f"ISSUES {repo or ''} {state}".replace("  ", " ")
        case ShowIssue(r):
            return f"ISSUE {ref(r)}"
        case ListPulls(repo):
            return f"PRS {repo or ''}".strip()
        case ShowPull(r):
            return f"PR {ref(r)}"
        case Search(query):
            return f"SEARCH {query}"
        case Summarize(repo, number, latest):
            target = f"{repo or ''}#{number}" if number else f"{repo or ''}{' ISSUE' if latest else ''}"
            return f"SUMMARIZE {target}".strip()
        case _:
            return type(command).__name__.upper()
