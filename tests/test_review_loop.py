"""Tests for the unattended multi-call review loop."""

import json
import sys
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import review_bot as rb  # noqa: E402


def _prepare_review(tmp_path, monkeypatch, provider, *, single_repo=False):
    workspace = tmp_path / "service-one" if single_repo else tmp_path / "workspace"
    checkout = workspace if single_repo else workspace / "service-one"
    (checkout / ".git").mkdir(parents=True)
    sibling = workspace / "service-two"
    (sibling / ".git").mkdir(parents=True)
    (sibling / "ok.py").write_text("ok content")
    monkeypatch.setattr(rb, "WORKSPACE_PATH", workspace)
    monkeypatch.setattr(rb, "detect_base_branch", mock.Mock(return_value="main"))
    monkeypatch.setattr(
        rb,
        "run_git",
        lambda path, command, *args: "diff line\n" if command == "diff" else "",
    )
    monkeypatch.setattr(rb.subprocess, "run", mock.Mock())
    worktree = (
        Path(rb.tempfile.gettempdir())
        / "review-bot-service-one-pr-review-42-abc123"
    )
    source = worktree / "src" / "app.py"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("w" * 5000)
    pr = mock.Mock(number=42, title="Test PR")
    pr.head.sha = "abc123"
    return pr, workspace, checkout


def test_read_request_fetches_worktree_file_and_posts_one_review(
    tmp_path, monkeypatch
):
    provider = mock.Mock(max_payload_chars=30000, name="ollama")
    provider.request_review.side_effect = [
        '<think>need the source</think>\n{"read":["service-one/src/app.py"]}',
        "## Bugs & Risks\n\n- None.",
    ]
    pr, _workspace, checkout = _prepare_review(tmp_path, monkeypatch, provider)
    (checkout / "src").mkdir(parents=True)
    (checkout / "src" / "app.py").write_text("clone source")

    assert rb.review_pr(pr, "owner/service-one", provider)

    assert provider.request_review.call_count == 2
    follow_up = provider.request_review.call_args.args[0]
    assert "w" * 4000 in follow_up
    assert "w" * 4001 not in follow_up
    assert "clone source" not in follow_up
    pr.create_issue_comment.assert_called_once()
    comment = pr.create_issue_comment.call_args.args[0]
    assert "## Bugs & Risks" in comment
    assert "need the source" not in comment


def test_read_request_ignores_paths_outside_the_workspace(tmp_path, monkeypatch):
    provider = mock.Mock(max_payload_chars=30000, name="ollama")
    secret = tmp_path / "secret.txt"
    secret.write_text("secret contents")
    provider.request_review.side_effect = [
        json.dumps({
            "read": [
                "../secret.txt",
                "service-two/../../secret.txt",
                "service-two/ok.py",
            ]
        }),
        "## Fine\n\nNo issues.",
    ]
    pr, _workspace, _checkout = _prepare_review(tmp_path, monkeypatch, provider)

    assert rb.review_pr(pr, "owner/service-one", provider)

    follow_up = provider.request_review.call_args.args[0]
    assert "ok content" in follow_up
    assert "secret contents" not in follow_up


def test_read_request_keeps_only_the_first_eight_paths(tmp_path, monkeypatch):
    provider = mock.Mock(max_payload_chars=30000, name="ollama")
    pr, workspace, _checkout = _prepare_review(tmp_path, monkeypatch, provider)
    paths = []
    for index in range(9):
        relative = f"file{index}.py"
        (workspace / "service-two" / relative).write_text(f"FILE-{index}")
        paths.append(f"service-two/{relative}")
    provider.request_review.side_effect = [
        json.dumps({"read": paths}),
        "## Fine\n\nNo issues.",
    ]

    assert rb.review_pr(pr, "owner/service-one", provider)

    follow_up = provider.request_review.call_args.args[0]
    assert "FILE-7" in follow_up
    assert "FILE-8" not in follow_up


def test_overlong_payload_drops_files_before_digests_before_the_diff():
    diff = "diff line\n"
    digest = [("service-two", "DIGEST-BODY")]
    files = [("service-two/ok.py", "FILE-BODY" + ("f" * 400))]
    without_files = rb.assemble_review_payload(
        base_branch="main",
        diff_text=diff,
        rules=[],
        digests=digest,
        files=[],
        max_chars=10**6,
    )
    payload = rb.assemble_review_payload(
        base_branch="main",
        diff_text=diff,
        rules=[],
        digests=digest,
        files=files,
        max_chars=len(without_files),
    )

    assert "FILE-BODY" not in payload
    assert "DIGEST-BODY" in payload
    assert "diff line" in payload

    diff_only = rb.assemble_review_payload(
        base_branch="main",
        diff_text=diff,
        rules=[],
        digests=[],
        files=[],
        max_chars=10**6,
    )
    dropped = rb.assemble_review_payload(
        base_branch="main",
        diff_text=diff,
        rules=[],
        digests=digest,
        files=files,
        max_chars=len(diff_only),
    )

    assert "DIGEST-BODY" not in dropped
    assert "FILE-BODY" not in dropped
    assert "diff line" in dropped
    assert "[diff truncated]" not in dropped


@pytest.mark.parametrize(
    "reply",
    ["", "   ", "Is this intentional?", '{"read": ["nope"], "why": "because"}'],
)
def test_non_review_replies_are_not_posted(tmp_path, monkeypatch, reply):
    provider = mock.Mock(max_payload_chars=30000, name="ollama")
    provider.request_review.return_value = reply
    pr, _workspace, _checkout = _prepare_review(tmp_path, monkeypatch, provider)

    assert rb.review_pr(pr, "owner/service-one", provider) is False

    pr.create_issue_comment.assert_not_called()


def test_read_loop_stops_at_twelve_calls_without_posting(tmp_path, monkeypatch):
    provider = mock.Mock(max_payload_chars=30000, name="ollama")
    provider.request_review.return_value = '{"read":["service-one/missing.py"]}'
    pr, _workspace, _checkout = _prepare_review(tmp_path, monkeypatch, provider)

    assert rb.review_pr(pr, "owner/service-one", provider) is False

    assert provider.request_review.call_count == 12
    pr.create_issue_comment.assert_not_called()


def test_unsettled_review_is_not_marked_reviewed(tmp_path, monkeypatch):
    workspace = tmp_path / "service-one"
    (workspace / ".git").mkdir(parents=True)
    monkeypatch.setattr(rb, "WORKSPACE_PATH", workspace)
    monkeypatch.setattr(rb, "GITHUB_TOKEN", "test-token")
    monkeypatch.setattr(rb, "GITHUB_USER", "reviewer")
    monkeypatch.setattr(rb, "GITHUB_OWNER", "owner")
    monkeypatch.setattr(rb, "GITHUB_ORG", "")
    state_file = tmp_path / "state.txt"
    monkeypatch.setattr(rb, "STATE_FILE", state_file)
    monkeypatch.setattr(rb, "detect_base_branch", mock.Mock(return_value="main"))
    monkeypatch.setattr(
        rb,
        "run_git",
        lambda path, command, *args: "diff line\n" if command == "diff" else "",
    )
    monkeypatch.setattr(rb.subprocess, "run", mock.Mock())

    issue = mock.Mock()
    issue.repository.name = "service-one"
    issue.repository.full_name = "owner/service-one"
    pr = issue.repository.get_pull.return_value
    pr.number = 42
    pr.head.sha = "abc123"
    pr.get_issue_comments.return_value = []
    github = mock.Mock()
    github.get_user.return_value.type = "User"
    github.search_issues.return_value = [issue]
    monkeypatch.setattr(rb, "Github", mock.Mock(return_value=github))
    provider = mock.Mock(max_payload_chars=30000, name="ollama")
    provider.request_review.return_value = "Is this intentional?"
    monkeypatch.setattr(rb, "load_ai_provider", mock.Mock(return_value=provider))

    assert rb.main() == 1

    pr.create_issue_comment.assert_not_called()
    assert not state_file.exists()
