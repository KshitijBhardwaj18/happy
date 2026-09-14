"""Tests for tools/github.py. All PyGithub objects are mocked (unittest.mock);
no real network calls are made."""
from __future__ import annotations

import subprocess
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from github import GithubException

from tools import github as gh


@pytest.fixture(autouse=True)
def _reset_module_cache():
    """Every test starts with a clean module-level client/repo cache."""
    gh._reset_cache()
    yield
    gh._reset_cache()


def _fake_settings(github_token="tok-123", github_repo="acme/widgets"):
    return SimpleNamespace(github_token=github_token, github_repo=github_repo)


# --- token resolution -------------------------------------------------------


def test_github_token_prefers_settings(monkeypatch):
    monkeypatch.setattr(gh, "load_settings", lambda: _fake_settings(github_token="settings-token"))
    run_mock = MagicMock()
    monkeypatch.setattr(gh.subprocess, "run", run_mock)

    assert gh._github_token() == "settings-token"
    run_mock.assert_not_called()


def test_github_token_falls_back_to_gh_cli(monkeypatch):
    monkeypatch.setattr(gh, "load_settings", lambda: _fake_settings(github_token=""))
    monkeypatch.setattr(
        gh.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(returncode=0, stdout="cli-token\n"),
    )

    assert gh._github_token() == "cli-token"


def test_github_token_falls_back_to_empty_when_gh_cli_fails(monkeypatch):
    monkeypatch.setattr(gh, "load_settings", lambda: _fake_settings(github_token=""))

    def _raise(*a, **k):
        raise FileNotFoundError("gh not found")

    monkeypatch.setattr(gh.subprocess, "run", _raise)

    assert gh._github_token() == ""


def test_github_token_falls_back_to_empty_when_gh_cli_errors(monkeypatch):
    monkeypatch.setattr(gh, "load_settings", lambda: _fake_settings(github_token=""))
    monkeypatch.setattr(
        gh.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(returncode=1, stdout=""),
    )

    assert gh._github_token() == ""


def test_client_instance_is_cached(monkeypatch):
    monkeypatch.setattr(gh, "load_settings", lambda: _fake_settings())
    monkeypatch.setattr(gh, "Github", lambda *a, **k: MagicMock(name="github-client"))

    first = gh._client_instance()
    second = gh._client_instance()
    assert first is second


def _patch_repo(monkeypatch, fake_repo):
    monkeypatch.setattr(gh, "load_settings", lambda: _fake_settings())
    monkeypatch.setattr(gh, "_repo", lambda: fake_repo)


# --- recent_commits -----------------------------------------------------------


def _fake_commit(sha, message, author_login, date, files):
    git_author = SimpleNamespace(name=author_login, date=date)
    git_commit = SimpleNamespace(message=message, author=git_author)
    return SimpleNamespace(
        sha=sha,
        commit=git_commit,
        author=SimpleNamespace(login=author_login),
        files=[SimpleNamespace(filename=f) for f in files],
    )


def test_recent_commits_filters_by_time_window(monkeypatch):
    now = datetime.now(timezone.utc)
    recent = _fake_commit("aaaaaaa1111", "fix: bump replicas\n", "kshitij", now - timedelta(hours=1), ["infra/k8s/checkout.yaml"])
    stale = _fake_commit("bbbbbbb2222", "old change", "someone", now - timedelta(hours=100), ["infra/k8s/frontend.yaml"])

    fake_repo = MagicMock()
    # Simulate a server that ignores `since` and returns everything, so we can
    # confirm the client-side window filter actually does the filtering.
    fake_repo.get_commits.return_value = [recent, stale]
    _patch_repo(monkeypatch, fake_repo)

    result = gh.recent_commits(path_prefix="infra/k8s/", hours=24)

    assert result["ok"] is True
    shas = [c["sha"] for c in result["commits"]]
    assert shas == ["aaaaaaa"]
    assert result["commits"][0]["message"] == "fix: bump replicas"
    assert result["commits"][0]["author"] == "kshitij"
    assert result["commits"][0]["files"] == ["infra/k8s/checkout.yaml"]
    assert result["commits"][0]["committed_at"].endswith("Z")
    fake_repo.get_commits.assert_called_once()
    _, kwargs = fake_repo.get_commits.call_args
    assert kwargs["path"] == "infra/k8s/"


def test_recent_commits_falls_back_to_git_author_name_when_no_github_user(monkeypatch):
    now = datetime.now(timezone.utc)
    commit = _fake_commit("ccccccc3333", "no linked user", "git-name", now - timedelta(minutes=5), [])
    commit.author = None  # unlinked git author -> fall back to commit.commit.author.name

    fake_repo = MagicMock()
    fake_repo.get_commits.return_value = [commit]
    _patch_repo(monkeypatch, fake_repo)

    result = gh.recent_commits()
    assert result["ok"] is True
    assert result["commits"][0]["author"] == "git-name"


def test_recent_commits_handles_github_exception(monkeypatch):
    fake_repo = MagicMock()
    fake_repo.get_commits.side_effect = GithubException(500, data={"message": "boom"})
    _patch_repo(monkeypatch, fake_repo)

    result = gh.recent_commits()
    assert result == {"ok": False, "error": str(GithubException(500, data={"message": "boom"}))}


# --- recent_prs -----------------------------------------------------------


def _fake_pr(number, title, merged_at, author_login, updated_at, files):
    return SimpleNamespace(
        number=number,
        title=title,
        merged_at=merged_at,
        user=SimpleNamespace(login=author_login),
        updated_at=updated_at,
        get_files=lambda: [SimpleNamespace(filename=f) for f in files],
    )


def test_recent_prs_filters_by_time_window(monkeypatch):
    now = datetime.now(timezone.utc)
    fresh = _fake_pr(42, "Add chaos script", None, "kshitij", now - timedelta(hours=2), ["scripts/chaos.sh"])
    old = _fake_pr(10, "Ancient PR", now - timedelta(days=30), "someone", now - timedelta(hours=200), ["README.md"])

    fake_repo = MagicMock()
    fake_repo.get_pulls.return_value = [fresh, old]
    _patch_repo(monkeypatch, fake_repo)

    result = gh.recent_prs(state="all", hours=48)

    assert result["ok"] is True
    numbers = [pr["number"] for pr in result["prs"]]
    assert numbers == [42]
    assert result["prs"][0]["title"] == "Add chaos script"
    assert result["prs"][0]["merged_at"] is None
    assert result["prs"][0]["author"] == "kshitij"
    assert result["prs"][0]["files"] == ["scripts/chaos.sh"]
    fake_repo.get_pulls.assert_called_once()
    _, kwargs = fake_repo.get_pulls.call_args
    assert kwargs["state"] == "all"


def test_recent_prs_includes_merged_at_iso(monkeypatch):
    now = datetime.now(timezone.utc)
    merged = now - timedelta(hours=1)
    pr = _fake_pr(7, "Fix leak", merged, "author", now - timedelta(minutes=30), [])

    fake_repo = MagicMock()
    fake_repo.get_pulls.return_value = [pr]
    _patch_repo(monkeypatch, fake_repo)

    result = gh.recent_prs()
    assert result["prs"][0]["merged_at"] == gh._iso(merged)


def test_recent_prs_handles_github_exception(monkeypatch):
    fake_repo = MagicMock()
    fake_repo.get_pulls.side_effect = GithubException(403, data={"message": "rate limited"})
    _patch_repo(monkeypatch, fake_repo)

    result = gh.recent_prs()
    assert result["ok"] is False
    assert "403" in result["error"] or "rate limited" in result["error"]


# --- pr_summary -----------------------------------------------------------


def test_pr_summary_truncates_diff_and_limits_files(monkeypatch):
    long_patch = "\n".join(f"+line {i}" for i in range(100))
    files = [SimpleNamespace(filename=f"file{i}.yaml", patch=long_patch) for i in range(5)]
    pr = SimpleNamespace(
        title="Bump memory limits",
        body="Increases limits to avoid OOM",
        merged_at=None,
        get_files=lambda: files,
    )
    fake_repo = MagicMock()
    fake_repo.get_pull.return_value = pr
    _patch_repo(monkeypatch, fake_repo)

    result = gh.pr_summary(5)

    assert result["ok"] is True
    assert result["title"] == "Bump memory limits"
    assert len(result["files"]) == 5  # all filenames still listed
    assert len(result["diff_excerpt"]) == 3  # but only 3 files get patches
    for excerpt in result["diff_excerpt"]:
        assert len(excerpt["patch"].splitlines()) == 60
    fake_repo.get_pull.assert_called_once_with(5)


def test_pr_summary_handles_missing_patch(monkeypatch):
    files = [SimpleNamespace(filename="binary.png", patch=None)]
    pr = SimpleNamespace(title="t", body="b", merged_at=None, get_files=lambda: files)
    fake_repo = MagicMock()
    fake_repo.get_pull.return_value = pr
    _patch_repo(monkeypatch, fake_repo)

    result = gh.pr_summary(1)
    assert result["ok"] is True
    assert result["diff_excerpt"] == [{"filename": "binary.png", "patch": ""}]


def test_pr_summary_handles_github_exception(monkeypatch):
    fake_repo = MagicMock()
    fake_repo.get_pull.side_effect = GithubException(404, data={"message": "not found"})
    _patch_repo(monkeypatch, fake_repo)

    result = gh.pr_summary(999)
    assert result == {"ok": False, "error": str(GithubException(404, data={"message": "not found"}))}


# --- open_markdown_pr -----------------------------------------------------------


def test_open_markdown_pr_creates_new_branch_and_file(monkeypatch):
    fake_repo = MagicMock()
    fake_repo.default_branch = "main"

    fake_repo.get_git_ref.side_effect = GithubException(404, data={"message": "not found"})
    base_ref = SimpleNamespace(object=SimpleNamespace(sha="base-sha"))

    def get_git_ref(ref):
        if ref == "heads/main":
            return base_ref
        raise GithubException(404, data={"message": "not found"})

    fake_repo.get_git_ref.side_effect = get_git_ref
    fake_repo.get_contents.side_effect = GithubException(404, data={"message": "not found"})
    fake_pr = SimpleNamespace(html_url="https://github.com/acme/widgets/pull/99", number=99)
    fake_repo.create_pull.return_value = fake_pr

    _patch_repo(monkeypatch, fake_repo)

    result = gh.open_markdown_pr(
        branch="happy/postmortem-abc",
        path="docs/postmortems/2026-09-14-checkout-oom.md",
        content="# Postmortem",
        title="Postmortem: checkout OOM",
        body="See summary.",
    )

    assert result == {"ok": True, "url": "https://github.com/acme/widgets/pull/99", "number": 99}
    fake_repo.create_git_ref.assert_called_once_with(ref="refs/heads/happy/postmortem-abc", sha="base-sha")
    fake_repo.create_file.assert_called_once()
    fake_repo.update_file.assert_not_called()
    fake_repo.create_pull.assert_called_once_with(
        base="main", head="happy/postmortem-abc", title="Postmortem: checkout OOM", body="See summary."
    )


def test_open_markdown_pr_reuses_existing_branch_and_updates_file(monkeypatch):
    fake_repo = MagicMock()
    fake_repo.default_branch = "main"
    fake_repo.get_git_ref.return_value = SimpleNamespace(object=SimpleNamespace(sha="existing-sha"))
    fake_repo.get_contents.return_value = SimpleNamespace(sha="file-sha")
    fake_pr = SimpleNamespace(html_url="https://github.com/acme/widgets/pull/100", number=100)
    fake_repo.create_pull.return_value = fake_pr

    _patch_repo(monkeypatch, fake_repo)

    result = gh.open_markdown_pr(
        branch="happy/runbook-xyz",
        path="docs/runbooks/checkout-oom.md",
        content="# Runbook",
        title="Runbook: checkout OOM",
        body="Fix steps.",
    )

    assert result["ok"] is True
    fake_repo.create_git_ref.assert_not_called()
    fake_repo.update_file.assert_called_once()
    args, kwargs = fake_repo.update_file.call_args
    assert args[3] == "file-sha"
    fake_repo.create_file.assert_not_called()


def test_open_markdown_pr_returns_error_dict_on_failure(monkeypatch):
    fake_repo = MagicMock()
    fake_repo.default_branch = "main"
    fake_repo.get_git_ref.side_effect = GithubException(500, data={"message": "server error"})

    _patch_repo(monkeypatch, fake_repo)

    result = gh.open_markdown_pr(branch="b", path="p.md", content="c", title="t", body="b")
    assert result["ok"] is False
    assert "error" in result
