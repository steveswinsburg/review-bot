"""Unit tests for state management and the GitHub comment-marker fallback."""

import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import review_bot as rb  # noqa: E402


def _fake_comment(login, body):
    comment = mock.Mock()
    comment.user = mock.Mock()
    comment.user.login = login
    comment.body = body
    return comment


def test_state_marker_round_trip():
    key = "org/repo#42:abc123"
    marker = rb.state_marker(key)
    assert marker == "<!-- review-bot:state:org/repo#42:abc123 -->"
    assert marker.startswith(rb.STATE_MARKER_PREFIX)
    assert marker.endswith(rb.STATE_MARKER_SUFFIX)


def test_load_reviewed_states_missing_file(tmp_path, monkeypatch):
    monkeypatch.setattr(rb, "STATE_FILE", tmp_path / "reviewed_prs.txt")
    assert rb.load_reviewed_states() == set()


def test_mark_and_load_reviewed_states(tmp_path, monkeypatch):
    state_file = tmp_path / "reviewed_prs.txt"
    monkeypatch.setattr(rb, "STATE_FILE", state_file)
    rb.mark_state_reviewed("org/repo#1:sha1")
    rb.mark_state_reviewed("org/repo#2:sha2")
    assert rb.load_reviewed_states() == {"org/repo#1:sha1", "org/repo#2:sha2"}


def test_load_reviewed_states_ignores_blank_and_strips(tmp_path, monkeypatch):
    state_file = tmp_path / "reviewed_prs.txt"
    state_file.write_text("  org/repo#1:sha1  \n\n\norg/repo#2:sha2\n")
    monkeypatch.setattr(rb, "STATE_FILE", state_file)
    assert rb.load_reviewed_states() == {"org/repo#1:sha1", "org/repo#2:sha2"}


def test_already_reviewed_on_github_finds_marker(monkeypatch):
    monkeypatch.setattr(rb, "GITHUB_USER", "the-bot")
    key = "org/repo#7:deadbeef"
    pr = mock.Mock()
    pr.get_issue_comments.return_value = [
        _fake_comment("someone-else", "LGTM"),
        _fake_comment("the-bot", f"{rb.state_marker(key)}\n## Review"),
    ]
    assert rb.already_reviewed_on_github(pr, key) is True


def test_already_reviewed_on_github_new_sha_not_matched(monkeypatch):
    """A new commit SHA must be treated as unreviewed even if an older
    review comment exists, so updated PRs are re-reviewed."""
    monkeypatch.setattr(rb, "GITHUB_USER", "the-bot")
    old_key = "org/repo#7:oldsha"
    new_key = "org/repo#7:newsha"
    pr = mock.Mock()
    pr.get_issue_comments.return_value = [
        _fake_comment("the-bot", f"{rb.state_marker(old_key)}\n## Review"),
    ]
    assert rb.already_reviewed_on_github(pr, new_key) is False


def test_already_reviewed_on_github_ignores_other_users(monkeypatch):
    monkeypatch.setattr(rb, "GITHUB_USER", "the-bot")
    key = "org/repo#7:deadbeef"
    pr = mock.Mock()
    pr.get_issue_comments.return_value = [
        _fake_comment("impostor", rb.state_marker(key)),
    ]
    assert rb.already_reviewed_on_github(pr, key) is False


def test_already_reviewed_on_github_fails_open(monkeypatch):
    monkeypatch.setattr(rb, "GITHUB_USER", "the-bot")
    pr = mock.Mock()
    pr.get_issue_comments.side_effect = rb.GithubException(500, "boom", {})
    assert rb.already_reviewed_on_github(pr, "org/repo#7:sha") is False
