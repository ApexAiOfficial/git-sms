"""Read-only GitHub repository and issue summarization."""
from __future__ import annotations
import base64
import logging
from functools import lru_cache
from typing import Any
import requests
from commands import REQUEST_TIMEOUT, get_headers
from llm_client import LLMNotConfiguredError, complete_text

log = logging.getLogger(__name__)
GITHUB_API = "https://api.github.com"
MAX_TOKENS = 16000
SUMMARY_NOT_CONFIGURED = "AI summarization is not configured. Set GITHUB_OPENAI_API_KEY or OPENAI_API_KEY."

@lru_cache(maxsize=1)
def get_encoder() -> Any:
    import tiktoken
    return tiktoken.get_encoding("cl100k_base")

def num_tokens(text: str) -> int:
    return len(get_encoder().encode(text))

def truncate_text(text: str, token_limit: int) -> str:
    encoder = get_encoder()
    return encoder.decode(encoder.encode(text)[:max(0, token_limit)])

def _json(response: requests.Response) -> Any | None:
    try:
        return response.json()
    except ValueError:
        return None

def _error(response: requests.Response, noun: str) -> str:
    if response.status_code == 404:
        return f"Could not find that {noun}."
    if response.status_code in {403, 429}:
        return "GitHub API rate limit or access restriction encountered. Try again later."
    return f"GitHub could not return that {noun}."

def _summarize(prompt: str) -> str:
    try:
        result = complete_text(system_prompt="Summarize GitHub content in plain text under 1,000 characters.", user_prompt=prompt, temperature=0.2, max_tokens=700)
    except LLMNotConfiguredError:
        return SUMMARY_NOT_CONFIGURED
    except Exception:
        log.exception("LLM summarization failed")
        return "AI summarization failed. Please try again later."
    return result if len(result) <= 1000 else "The summary was too long to send. Try summarizing less content."

def summarize_any_repo(repo_full_name: str) -> str:
    if repo_full_name.count("/") != 1:
        return "Invalid format. Use <owner>/<repo>."
    repo_url = f"{GITHUB_API}/repos/{repo_full_name}"
    try:
        repo_response = requests.get(repo_url, headers=get_headers(), timeout=REQUEST_TIMEOUT)
        if not repo_response.ok:
            return _error(repo_response, "repository")
        repo = _json(repo_response)
        if not isinstance(repo, dict):
            return "GitHub returned an invalid repository response."
        readme_response = requests.get(f"{repo_url}/readme", headers=get_headers(), timeout=REQUEST_TIMEOUT)
        readme_text = "No README content available."
        if readme_response.ok:
            readme = _json(readme_response)
            encoded = readme.get("content") if isinstance(readme, dict) else None
            if encoded:
                try:
                    readme_text = base64.b64decode(encoded).decode("utf-8", errors="ignore")
                except (ValueError, TypeError):
                    readme_text = "README content could not be decoded."
    except requests.Timeout:
        return "GitHub did not respond in time. Please try again."
    except requests.RequestException:
        log.exception("GitHub repository request failed")
        return "Could not reach GitHub. Please try again later."
    prefix = ("Summarize this GitHub repository:\n\n" f"Repo Name: {repo.get('name') or 'Unknown'}\n" f"Owner: {(repo.get('owner') or {}).get('login') or 'Unknown'}\n" f"Description: {repo.get('description') or 'No description'}\n" f"Stars: {repo.get('stargazers_count', 0)}\n" f"Forks: {repo.get('forks_count', 0)}\n" f"Primary Language: {repo.get('language') or 'Unknown'}\n\nREADME:\n")
    try:
        available = MAX_TOKENS - num_tokens(prefix)
        if num_tokens(readme_text) > available:
            readme_text = truncate_text(readme_text, available)
    except Exception:
        log.exception("Tokenizer failed")
        return "AI summarization is temporarily unavailable. Please try again later."
    return _summarize(prefix + readme_text)

def summarize_latest_issue(repo_full_name: str) -> str:
    if repo_full_name.count("/") != 1:
        return "Invalid format. Use <owner>/<repo>."
    try:
        response = requests.get(f"{GITHUB_API}/repos/{repo_full_name}/issues", headers=get_headers(), params={"state": "open", "per_page": 10}, timeout=REQUEST_TIMEOUT)
        if not response.ok:
            return _error(response, "issue list")
        payload = _json(response)
        if not isinstance(payload, list):
            return "GitHub returned an invalid issue response."
    except requests.Timeout:
        return "GitHub did not respond in time. Please try again."
    except requests.RequestException:
        return "Could not reach GitHub. Please try again later."
    issue = next((item for item in payload if isinstance(item, dict) and "pull_request" not in item), None)
    return summarize_specific_issue(repo_full_name, int(issue["number"])) if issue else "No open issues found."

def summarize_specific_issue(repo_full_name: str, issue_number: int) -> str:
    if repo_full_name.count("/") != 1:
        return "Invalid format. Use <owner>/<repo>."
    if issue_number < 1:
        return "Issue number must be positive."
    base = f"{GITHUB_API}/repos/{repo_full_name}/issues/{issue_number}"
    try:
        issue_response = requests.get(base, headers=get_headers(), timeout=REQUEST_TIMEOUT)
        if not issue_response.ok:
            return _error(issue_response, "issue")
        comments_response = requests.get(f"{base}/comments", headers=get_headers(), timeout=REQUEST_TIMEOUT)
        if not comments_response.ok:
            return _error(comments_response, "issue comments")
        issue, comments = _json(issue_response), _json(comments_response)
        if not isinstance(issue, dict) or not isinstance(comments, list):
            return "GitHub returned an invalid issue response."
    except requests.Timeout:
        return "GitHub did not respond in time. Please try again."
    except requests.RequestException:
        return "Could not reach GitHub. Please try again later."
    thread = [f"Issue #{issue_number}: {issue.get('title') or 'Untitled'}", str(issue.get("body") or "")]
    thread.extend(str(comment.get("body") or "") for comment in comments if isinstance(comment, dict))
    return _summarize("Summarize this GitHub issue thread:\n" + "\n".join(thread))
