"""GitHub tools: recent commits/PRs for correlating incidents with changes,
plus a plain helper to open a markdown PR (used by postmortem/runbook, PR 12).

Token resolution order: `Settings.github_token`, else `gh auth token` (subprocess),
else an unauthenticated client (rate-limited, read-only-friendly). The PyGithub
client is built lazily and cached at module level so repeated tool calls in one
process reuse the same connection.
"""
from __future__ import annotations

import logging
import subprocess
from datetime import datetime, timedelta, timezone

from github import Github, GithubException
from github.Repository import Repository
from strands import tool

from config import load_settings

logger = logging.getLogger(__name__)

_client: Github | None = None
_repo_cache: Repository | None = None

_MAX_PATCH_LINES = 60
_MAX_PATCH_FILES = 3


def _github_token() -> str:
    """Resolve a GitHub token: settings, then `gh auth token`, else empty string."""
    settings = load_settings()
    if settings.github_token:
        return settings.github_token
    if settings.use_identity:
        from identity import resolve_api_key

        token = resolve_api_key(settings.github_credential_name)
        if token:
            return token
    try:
        result = subprocess.run(
            ["gh", "auth", "token"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        token = result.stdout.strip()
        if result.returncode == 0 and token:
            return token
    except (OSError, subprocess.SubprocessError) as exc:
        logger.info("gh auth token unavailable: %s", exc)
    return ""


def _client_instance() -> Github:
    """Return a cached, module-level PyGithub client, creating it on first use."""
    global _client
    if _client is None:
        token = _github_token()
        if token:
            _client = Github(token)
        else:
            logger.info("No GitHub token found; using unauthenticated client")
            _client = Github()
    return _client


def _repo() -> Repository:
    """Return a cached Repository handle for `Settings.github_repo`."""
    global _repo_cache
    if _repo_cache is None:
        settings = load_settings()
        _repo_cache = _client_instance().get_repo(settings.github_repo)
    return _repo_cache


def _reset_cache() -> None:
    """For tests: drop cached client/repo so the next call rebuilds them."""
    global _client, _repo_cache
    _client = None
    _repo_cache = None


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


@tool
def recent_commits(path_prefix: str = "infra/k8s/", hours: int = 24) -> dict:
    """Return commits touching `path_prefix` in the last `hours`, newest first.

    Use this to correlate an incident's start time with recent infrastructure or
    application changes. Each entry has `sha` (short), `message` (first line),
    `author`, `committed_at` (ISO UTC), and `files` (paths changed in that commit).
    Returns `{"ok": False, "error": ...}` on failure instead of raising.
    """
    try:
        repo = _repo()
        since = datetime.now(timezone.utc) - timedelta(hours=hours)
        commits = repo.get_commits(path=path_prefix, since=since)
        results = []
        for commit in commits:
            git_commit = commit.commit
            committed_at = git_commit.author.date
            if committed_at.tzinfo is None:
                committed_at = committed_at.replace(tzinfo=timezone.utc)
            if committed_at < since:
                # Belt and suspenders: the API is asked to filter by `since` already,
                # but a mocked/misbehaving client might return everything.
                continue
            author_login = commit.author.login if commit.author else git_commit.author.name
            results.append(
                {
                    "sha": commit.sha[:7],
                    "message": git_commit.message.splitlines()[0] if git_commit.message else "",
                    "author": author_login,
                    "committed_at": _iso(committed_at),
                    "files": [f.filename for f in commit.files],
                }
            )
        return {"ok": True, "commits": results}
    except GithubException as exc:
        logger.info("recent_commits failed: %s", exc)
        return {"ok": False, "error": str(exc)}
    except Exception as exc:  # noqa: BLE001 - tool contract: never raise
        logger.info("recent_commits failed: %s", exc)
        return {"ok": False, "error": str(exc)}


@tool
def recent_prs(state: str = "all", hours: int = 48) -> dict:
    """Return pull requests updated in the last `hours`, newest first.

    `state` is one of "open", "closed", or "all" (default). Each entry has
    `number`, `title`, `merged_at` (ISO UTC or None), `author`, and `files`
    (paths changed). Use this alongside `recent_commits` to find the change
    that likely triggered an incident. Returns `{"ok": False, "error": ...}`
    on failure instead of raising.
    """
    try:
        repo = _repo()
        since = datetime.now(timezone.utc) - timedelta(hours=hours)
        pulls = repo.get_pulls(state=state, sort="updated", direction="desc")
        results = []
        for pr in pulls:
            updated_at = getattr(pr, "updated_at", None)
            if updated_at is None:
                continue
            if updated_at.tzinfo is None:
                updated_at = updated_at.replace(tzinfo=timezone.utc)
            if updated_at < since:
                continue
            results.append(
                {
                    "number": pr.number,
                    "title": pr.title,
                    "merged_at": _iso(pr.merged_at),
                    "author": pr.user.login if pr.user else None,
                    "files": [f.filename for f in pr.get_files()],
                }
            )
        return {"ok": True, "prs": results}
    except GithubException as exc:
        logger.info("recent_prs failed: %s", exc)
        return {"ok": False, "error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        logger.info("recent_prs failed: %s", exc)
        return {"ok": False, "error": str(exc)}


def _truncate_patch(patch: str | None) -> str:
    if not patch:
        return ""
    lines = patch.splitlines()
    return "\n".join(lines[:_MAX_PATCH_LINES])


@tool
def pr_summary(number: int) -> dict:
    """Return a detailed summary of pull request `number`.

    Includes `title`, `body`, `merged_at` (ISO UTC or None), `files` (paths
    changed), and `diff_excerpt` (a truncated patch per file: at most the
    first 60 lines each, for at most 3 files) so an agent can read what
    actually changed without pulling the full diff. Returns
    `{"ok": False, "error": ...}` on failure instead of raising.
    """
    try:
        repo = _repo()
        pr = repo.get_pull(number)
        files = list(pr.get_files())
        diff_excerpt = [
            {"filename": f.filename, "patch": _truncate_patch(f.patch)}
            for f in files[:_MAX_PATCH_FILES]
        ]
        return {
            "ok": True,
            "title": pr.title,
            "body": pr.body,
            "merged_at": _iso(pr.merged_at),
            "files": [f.filename for f in files],
            "diff_excerpt": diff_excerpt,
        }
    except GithubException as exc:
        logger.info("pr_summary failed: %s", exc)
        return {"ok": False, "error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        logger.info("pr_summary failed: %s", exc)
        return {"ok": False, "error": str(exc)}


def open_markdown_pr(branch: str, path: str, content: str, title: str, body: str) -> dict:
    """Create (or reuse) `branch` off the default branch, write `content` to `path`,
    and open a PR. Not a Strands tool -- called directly by postmortem/runbook code.

    Idempotent: if `branch` already exists, it is reused as-is (not reset to the
    default branch head). If `path` already exists on the branch, it is updated
    in place; otherwise it is created. Returns `{"ok": True, "url": ..., "number": ...}`
    or `{"ok": False, "error": ...}`.
    """
    try:
        repo = _repo()
        default_branch = repo.default_branch
        ref_name = f"refs/heads/{branch}"

        branch_exists = True
        try:
            repo.get_git_ref(f"heads/{branch}")
        except GithubException as exc:
            if exc.status == 404:
                branch_exists = False
            else:
                raise

        if not branch_exists:
            base_ref = repo.get_git_ref(f"heads/{default_branch}")
            repo.create_git_ref(ref=ref_name, sha=base_ref.object.sha)

        try:
            existing = repo.get_contents(path, ref=branch)
            sha = existing.sha if hasattr(existing, "sha") else existing[0].sha
            repo.update_file(path, title, content, sha, branch=branch)
        except GithubException as exc:
            if exc.status == 404:
                repo.create_file(path, title, content, branch=branch)
            else:
                raise

        pr = repo.create_pull(base=default_branch, head=branch, title=title, body=body)
        return {"ok": True, "url": pr.html_url, "number": pr.number}
    except GithubException as exc:
        logger.info("open_markdown_pr failed: %s", exc)
        return {"ok": False, "error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        logger.info("open_markdown_pr failed: %s", exc)
        return {"ok": False, "error": str(exc)}
