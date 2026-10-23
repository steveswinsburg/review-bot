"""Tests for selecting repositories from the configured local workspace."""

import sys
from pathlib import Path
from unittest import mock

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


@pytest.mark.parametrize("git_file", [False, True])
def test_single_checkout_takes_precedence_over_nested_repositories(
    tmp_path, monkeypatch, git_file
):
    checkout = tmp_path / "service-one"
    checkout.mkdir()
    if git_file:
        (checkout / ".git").write_text("gitdir: ../.git/worktrees/service-one")
    else:
        (checkout / ".git").mkdir()
    (checkout / "nested" / ".git").mkdir(parents=True)
    (checkout / "nested" / "README.md").write_text("Unrelated nested context")
    (tmp_path / "sibling" / ".git").mkdir(parents=True)
    (tmp_path / "sibling" / "README.md").write_text("Unrelated sibling context")
    monkeypatch.setattr(rb, "WORKSPACE_PATH", checkout)

    assert rb.list_workspace_repositories() == [checkout]
    assert rb.collect_cross_repo_context("service-one") == ""


def test_empty_workspace_has_no_repositories(tmp_path, monkeypatch):
    monkeypatch.setattr(rb, "WORKSPACE_PATH", tmp_path)

    assert rb.list_workspace_repositories() == []


def test_multi_repo_context_includes_only_other_direct_git_clones(tmp_path, monkeypatch):
    (tmp_path / "service-one" / ".git").mkdir(parents=True)
    (tmp_path / "service-one" / "README.md").write_text("Current repo context")
    (tmp_path / "service-two" / ".git").mkdir(parents=True)
    (tmp_path / "service-two" / "README.md").write_text("Related repo context")
    (tmp_path / "other" / "nested" / ".git").mkdir(parents=True)
    (tmp_path / "other" / "README.md").write_text("Non-Git directory context")
    (tmp_path / "other" / "nested" / "README.md").write_text("Nested repo context")
    monkeypatch.setattr(rb, "WORKSPACE_PATH", tmp_path)

    context = rb.collect_cross_repo_context("service-one")

    assert "Related repo context" in context
    assert "Current repo context" not in context
    assert "Non-Git directory context" not in context
    assert "Nested repo context" not in context


def test_is_workspace_repository():
    workspace_repo_names = {"service-one"}

    assert rb.is_workspace_repository("service-one", workspace_repo_names)
    assert not rb.is_workspace_repository("service-two", workspace_repo_names)


@pytest.mark.parametrize("owner_type", ["User", "Organization"])
@pytest.mark.parametrize("single_repo", [False, True])
def test_main_reviews_only_selected_repositories(
    tmp_path, monkeypatch, owner_type, single_repo
):
    workspace = tmp_path / "service-one" if single_repo else tmp_path
    checkout = workspace if single_repo else workspace / "service-one"
    (checkout / ".git").mkdir(parents=True)
    monkeypatch.setattr(rb, "WORKSPACE_PATH", workspace)
    monkeypatch.setattr(rb, "GITHUB_TOKEN", "test-token")
    monkeypatch.setattr(rb, "GITHUB_USER", "reviewer")
    monkeypatch.setattr(rb, "GITHUB_OWNER", "owner")
    monkeypatch.setattr(rb, "GITHUB_ORG", "")
    monkeypatch.setattr(rb, "STATE_FILE", tmp_path / "state.txt")

    included = mock.Mock()
    included.repository.name = "service-one"
    included.repository.full_name = "owner/service-one"
    included_pr = included.repository.get_pull.return_value
    included_pr.user.login = "someone-else"
    included_pr.number = 42
    included_pr.head.sha = "abc123"
    included_pr.get_issue_comments.return_value = []
    excluded = mock.Mock()
    excluded.repository.name = "not-checked-out"
    github = mock.Mock()
    github.get_user.return_value.type = owner_type
    github.search_issues.return_value = [included, excluded]
    monkeypatch.setattr(rb, "Github", mock.Mock(return_value=github))
    provider = mock.Mock()
    monkeypatch.setattr(rb, "load_ai_provider", mock.Mock(return_value=provider))
    review = mock.Mock(return_value=True)
    monkeypatch.setattr(rb, "review_pr", review)

    assert rb.main() == 0

    qualifier = "user" if owner_type == "User" else "org"
    github.search_issues.assert_called_once_with(
        f"is:open is:pr {qualifier}:owner review-requested:reviewer"
    )
    review.assert_called_once_with(included_pr, "owner/service-one", provider)
    excluded.repository.get_pull.assert_not_called()


@pytest.mark.parametrize("single_repo", [False, True])
def test_review_pr_uses_workspace_layout_for_checkout_and_context(
    tmp_path, monkeypatch, single_repo
):
    workspace = tmp_path / "service-one" if single_repo else tmp_path
    checkout = workspace if single_repo else workspace / "service-one"
    (checkout / ".git").mkdir(parents=True)
    (workspace / "service-two" / ".git").mkdir(parents=True)
    (workspace / "service-two" / "README.md").write_text("Related repo context")
    monkeypatch.setattr(rb, "WORKSPACE_PATH", workspace)
    monkeypatch.setattr(rb, "detect_base_branch", mock.Mock(return_value="main"))
    run_git = mock.Mock(
        side_effect=lambda path, command, *args: "diff" if command == "diff" else ""
    )
    monkeypatch.setattr(rb, "run_git", run_git)
    monkeypatch.setattr(rb.subprocess, "run", mock.Mock())
    pr = mock.Mock(number=42, title="Test PR")
    pr.head.sha = "abc123"
    provider = mock.Mock(max_payload_chars=30000)
    provider.request_review.return_value = "Review result"

    assert rb.review_pr(pr, "owner/service-one", provider)

    assert run_git.call_args_list[0] == mock.call(checkout, "fetch", "origin", "main")
    prompt = provider.request_review.call_args.args[0]
    assert ("Related repo context" in prompt) is not single_repo
    pr.create_issue_comment.assert_called_once()


def test_main_logs_empty_workspace(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(rb, "WORKSPACE_PATH", tmp_path)
    monkeypatch.setattr(rb, "GITHUB_TOKEN", "test-token")
    monkeypatch.setattr(rb, "GITHUB_USER", "reviewer")
    monkeypatch.setattr(rb, "GITHUB_OWNER", "owner")
    github = mock.Mock()
    github.get_user.return_value.type = "User"
    monkeypatch.setattr(rb, "Github", mock.Mock(return_value=github))

    with caplog.at_level("INFO"):
        assert rb.main() == 0

    assert "nothing to review" in caplog.text
    github.search_issues.assert_not_called()
