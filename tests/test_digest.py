"""Tests for per-repo digests stored in the workspace root."""

import sys
import urllib.error
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import review_bot as rb  # noqa: E402


def test_digest_round_trip_ignores_digest_directory_as_a_repository(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    (workspace / "service-one" / ".git").mkdir(parents=True)
    monkeypatch.setattr(rb, "WORKSPACE_PATH", workspace)

    rb.write_digest("service-one", "abc123", "exports: Widget\n")

    assert rb.read_digest("service-one") == ("abc123", "exports: Widget\n")
    assert rb.list_workspace_repositories() == [workspace / "service-one"]


def test_digest_is_stale_when_missing_or_sha_differs(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    repo = workspace / "service-one"
    (repo / ".git").mkdir(parents=True)
    monkeypatch.setattr(rb, "WORKSPACE_PATH", workspace)
    monkeypatch.setattr(rb, "run_git", lambda path, *args: "new-sha\n")

    assert rb.digest_is_fresh(repo) is False

    rb.write_digest("service-one", "old-sha", "exports: Widget\n")

    assert rb.digest_is_fresh(repo) is False


def test_digest_is_fresh_when_sha_matches_head(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    repo = workspace / "service-one"
    (repo / ".git").mkdir(parents=True)
    monkeypatch.setattr(rb, "WORKSPACE_PATH", workspace)
    monkeypatch.setattr(rb, "run_git", lambda path, *args: "abc123\n")
    rb.write_digest("service-one", "abc123", "exports: Widget\n")

    assert rb.digest_is_fresh(repo) is True


def test_read_workspace_file_clips_a_file_inside_a_clone(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    repo = workspace / "service-one"
    (repo / ".git").mkdir(parents=True)
    source = repo / "src" / "app.py"
    source.parent.mkdir(parents=True)
    source.write_text("x" * 5000)
    monkeypatch.setattr(rb, "WORKSPACE_PATH", workspace)

    assert rb.read_workspace_file("service-one", "src/app.py") == "x" * 4000


@pytest.mark.parametrize(
    "relative_path",
    ["../secret.txt", "../../secret.txt", "/tmp/secret.txt"],
)
def test_read_workspace_file_rejects_paths_outside_the_workspace(
    tmp_path, monkeypatch, relative_path
):
    workspace = tmp_path / "workspace"
    repo = workspace / "service-one"
    (repo / ".git").mkdir(parents=True)
    (tmp_path / "secret.txt").write_text("secret")
    monkeypatch.setattr(rb, "WORKSPACE_PATH", workspace)

    assert rb.read_workspace_file("service-one", relative_path) is None


def _git_stub(path, command, *args):
    if command == "diff":
        return "diff line\n"
    if (command, *args) == ("rev-parse", "HEAD"):
        return "abc123\n"
    if command == "ls-files":
        return "api.py\n"
    return ""


def _prepare_two_repo_run(tmp_path, monkeypatch, *, issues):
    workspace = tmp_path / "workspace"
    (workspace / "service-one" / ".git").mkdir(parents=True)
    (workspace / "service-two" / ".git").mkdir(parents=True)
    monkeypatch.setattr(rb, "WORKSPACE_PATH", workspace)
    monkeypatch.setattr(rb, "GITHUB_TOKEN", "test-token")
    monkeypatch.setattr(rb, "GITHUB_USER", "reviewer")
    monkeypatch.setattr(rb, "GITHUB_OWNER", "owner")
    monkeypatch.setattr(rb, "GITHUB_ORG", "")
    monkeypatch.setattr(rb, "STATE_FILE", tmp_path / "state.txt")
    monkeypatch.setattr(rb, "run_git", _git_stub)
    sync = mock.Mock(return_value=0)
    monkeypatch.setattr(rb, "sync_workspace_repositories", sync)
    github = mock.Mock()
    github.get_user.return_value.type = "User"
    github.search_issues.return_value = issues
    monkeypatch.setattr(rb, "Github", mock.Mock(return_value=github))
    provider = mock.Mock(max_payload_chars=30000, name="ollama")
    monkeypatch.setattr(rb, "load_ai_provider", mock.Mock(return_value=provider))
    return workspace, provider, sync


def _pull_request_issue():
    issue = mock.Mock()
    issue.repository.name = "service-one"
    issue.repository.full_name = "owner/service-one"
    pr = issue.repository.get_pull.return_value
    pr.number = 42
    pr.head.sha = "abc123"
    pr.get_issue_comments.return_value = []
    return issue, pr


def test_review_run_writes_a_stale_sibling_digest_before_reviewing(
    tmp_path, monkeypatch
):
    issue, _pr = _pull_request_issue()
    _workspace, provider, sync = _prepare_two_repo_run(
        tmp_path, monkeypatch, issues=[issue]
    )
    events = []

    def digest_reply(prompt):
        events.append("digest")
        assert "api.py" in prompt
        assert "exports, routes, and types" in prompt
        return "<think>scan</think>\nexports: Widget"

    provider.request_review.side_effect = digest_reply

    def review(*_args):
        events.append("review")
        return True

    monkeypatch.setattr(rb, "review_pr", review)

    assert rb.main() == 0

    sync.assert_called_once()
    assert events == ["digest", "review"]
    assert rb.read_digest("service-two") == ("abc123", "exports: Widget\n")
    assert rb.read_digest("service-one") is None


def test_review_run_skips_a_fresh_sibling_digest(tmp_path, monkeypatch):
    issue, _pr = _pull_request_issue()
    _workspace, provider, _sync = _prepare_two_repo_run(
        tmp_path, monkeypatch, issues=[issue]
    )
    rb.write_digest("service-two", "abc123", "exports: Widget\n")
    review = mock.Mock(return_value=True)
    monkeypatch.setattr(rb, "review_pr", review)

    assert rb.main() == 0

    provider.request_review.assert_not_called()
    review.assert_called_once()


def test_review_run_with_nothing_to_review_does_not_build_digests(
    tmp_path, monkeypatch
):
    _workspace, provider, _sync = _prepare_two_repo_run(
        tmp_path, monkeypatch, issues=[]
    )
    review = mock.Mock(return_value=True)
    monkeypatch.setattr(rb, "review_pr", review)

    assert rb.main() == 0

    provider.request_review.assert_not_called()
    review.assert_not_called()


def test_digest_failure_still_posts_a_review_with_fresh_digests(
    tmp_path, monkeypatch, caplog
):
    workspace = tmp_path / "workspace"
    (workspace / "service-one" / ".git").mkdir(parents=True)
    (workspace / "service-two" / ".git").mkdir(parents=True)
    (workspace / "service-three" / ".git").mkdir(parents=True)
    monkeypatch.setattr(rb, "WORKSPACE_PATH", workspace)
    monkeypatch.setattr(rb, "GITHUB_TOKEN", "test-token")
    monkeypatch.setattr(rb, "GITHUB_USER", "reviewer")
    monkeypatch.setattr(rb, "GITHUB_OWNER", "owner")
    monkeypatch.setattr(rb, "GITHUB_ORG", "")
    monkeypatch.setattr(rb, "STATE_FILE", tmp_path / "state.txt")
    monkeypatch.setattr(rb, "run_git", _git_stub)
    monkeypatch.setattr(rb, "detect_base_branch", mock.Mock(return_value="main"))
    monkeypatch.setattr(rb, "sync_workspace_repositories", mock.Mock(return_value=0))
    monkeypatch.setattr(rb.subprocess, "run", mock.Mock())
    rb.write_digest("service-three", "abc123", "Fresh sibling digest\n")
    issue, pr = _pull_request_issue()
    github = mock.Mock()
    github.get_user.return_value.type = "User"
    github.search_issues.return_value = [issue]
    monkeypatch.setattr(rb, "Github", mock.Mock(return_value=github))
    provider = mock.Mock(max_payload_chars=30000, name="ollama")
    prompts = []

    def reply(prompt):
        prompts.append(prompt)
        if "exports, routes, and types" in prompt:
            raise urllib.error.URLError("down")
        return "## Fine\n\nNo issues."

    provider.request_review.side_effect = reply
    monkeypatch.setattr(rb, "load_ai_provider", mock.Mock(return_value=provider))

    with caplog.at_level("ERROR"):
        assert rb.main() == 0

    assert "service-two" in caplog.text
    pr.create_issue_comment.assert_called_once()
    assert any("Fresh sibling digest" in prompt for prompt in prompts)
    assert rb.read_digest("service-two") is None
