"""Tests for selecting repositories from the configured local workspace."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import review_bot as rb  # noqa: E402


def test_list_workspace_repositories_includes_only_direct_git_clones(
    tmp_path, monkeypatch
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "service-one" / ".git").mkdir(parents=True)
    (workspace / "service-two").mkdir()
    (workspace / "nested").mkdir()
    (workspace / "nested" / "service-three" / ".git").mkdir(parents=True)
    (workspace / "worktree" / ".git").parent.mkdir()
    (workspace / "worktree" / ".git").write_text(
        "gitdir: ../.git/worktrees/worktree"
    )
    monkeypatch.setattr(rb, "WORKSPACE_PATH", workspace)

    assert rb.list_workspace_repositories() == [
        workspace / "service-one",
        workspace / "worktree",
    ]


def test_organization_uses_git_clones_in_workspace(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "service-one" / ".git").mkdir(parents=True)
    monkeypatch.setattr(rb, "WORKSPACE_PATH", workspace)

    assert rb.get_repositories_to_review(is_organization=True) == [
        workspace / "service-one"
    ]


def test_personal_account_uses_workspace_as_single_checkout(tmp_path, monkeypatch):
    checkout = tmp_path / "service-one"
    (checkout / ".git").mkdir(parents=True)
    monkeypatch.setattr(rb, "WORKSPACE_PATH", checkout)

    assert rb.get_repositories_to_review(is_organization=False) == [checkout]


def test_personal_account_requires_workspace_to_be_git_checkout(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(rb, "WORKSPACE_PATH", tmp_path)

    with pytest.raises(ValueError, match="point directly to the repository"):
        rb.get_repositories_to_review(is_organization=False)


def test_is_workspace_repository():
    workspace_repo_names = {"service-one"}

    assert rb.is_workspace_repository("service-one", workspace_repo_names)
    assert not rb.is_workspace_repository("service-two", workspace_repo_names)
