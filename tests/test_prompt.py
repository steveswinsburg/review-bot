"""Tests for the diff, rule, and digest payload sent to the model."""

import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import review_bot as rb  # noqa: E402


def _worktree(repo_name, number, sha):
    pr_ref = f"pr-review-{number}-{sha[:12]}"
    return Path(rb.tempfile.gettempdir()) / f"review-bot-{repo_name}-{pr_ref}"


def _review(monkeypatch, workspace, diff="diff line\n", max_chars=30000, sha="abc123"):
    monkeypatch.setattr(rb, "WORKSPACE_PATH", workspace)
    monkeypatch.setattr(rb, "detect_base_branch", mock.Mock(return_value="main"))
    monkeypatch.setattr(
        rb,
        "run_git",
        lambda path, command, *args: diff if command == "diff" else "",
    )
    monkeypatch.setattr(rb.subprocess, "run", mock.Mock())
    provider = mock.Mock(max_payload_chars=max_chars)
    provider.request_review.return_value = "## Fine\n\nNo issues."
    pr = mock.Mock(number=42, title="Test PR")
    pr.head.sha = sha
    assert rb.review_pr(pr, "owner/service-one", provider)
    return provider.request_review.call_args.args[0]


def test_prompt_includes_worktree_rules_and_not_sibling_readme(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    (workspace / "service-one" / ".git").mkdir(parents=True)
    (workspace / "service-two" / ".git").mkdir(parents=True)
    (workspace / "service-two" / "README.md").write_text("Related repo context")
    monkeypatch.setattr(rb.tempfile, "gettempdir", lambda: str(tmp_path))
    worktree = _worktree("service-one", 42, "abc123")
    worktree.mkdir(parents=True)
    (worktree / "AGENTS.md").write_text("Prefer small functions.")
    (worktree / "CLAUDE.md").write_text("Claude rule.")
    (worktree / "CONTRIBUTING.md").write_text("Contributing rule.")
    (worktree / "ARCHITECTURE.md").write_text("Architecture rule.")
    rule = worktree / ".cursor" / "rules" / "style.md"
    rule.parent.mkdir(parents=True)
    rule.write_text("y" * 5000)
    try:
        prompt = _review(monkeypatch, workspace)
    finally:
        for path in sorted(worktree.rglob("*"), reverse=True):
            if path.is_file():
                path.unlink()
            elif path.is_dir():
                path.rmdir()
        worktree.rmdir()

    assert "Prefer small functions." in prompt
    assert "Claude rule." in prompt
    assert "Contributing rule." in prompt
    assert "Architecture rule." in prompt
    assert "y" * 4000 in prompt
    assert "y" * 4001 not in prompt
    assert "Related repo context" not in prompt


def test_prompt_includes_sibling_digest_only(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    (workspace / "service-one" / ".git").mkdir(parents=True)
    (workspace / "service-two" / ".git").mkdir(parents=True)
    monkeypatch.setattr(rb, "WORKSPACE_PATH", workspace)
    rb.write_digest("service-one", "self", "Own repo digest")
    rb.write_digest("service-two", "other", "Sibling digest body")

    prompt = _review(monkeypatch, workspace)

    assert "Sibling digest body" in prompt
    assert "Own repo digest" not in prompt


def test_single_checkout_prompt_has_no_sibling_digest(tmp_path, monkeypatch):
    workspace = tmp_path / "service-one"
    (workspace / ".git").mkdir(parents=True)
    monkeypatch.setattr(rb, "WORKSPACE_PATH", workspace)
    rb.write_digest("service-two", "other", "Sibling digest body")

    prompt = _review(monkeypatch, workspace)

    assert "Sibling digest body" not in prompt


def test_overlong_payload_drops_digest_before_the_diff():
    payload = rb.assemble_review_payload(
        base_branch="main",
        diff_text="diff line\n",
        rules=[],
        digests=[("service-two", "DIGEST-BODY")],
        files=[],
        max_chars=80,
    )

    assert "diff line" in payload
    assert "DIGEST-BODY" not in payload
    assert "[diff truncated]" not in payload


def test_overlong_diff_is_cut_with_a_marker():
    diff = "d" * 500
    payload = rb.assemble_review_payload(
        base_branch="main",
        diff_text=diff,
        rules=[],
        digests=[],
        files=[],
        max_chars=120,
    )

    assert payload.endswith("[diff truncated]")
    assert len(payload) <= 120
    assert "d" * 20 in payload
    assert diff not in payload
