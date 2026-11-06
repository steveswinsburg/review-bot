"""Local and GitHub records of commits that already have a review."""

import logging

from github import GithubException

log = logging.getLogger("review-bot")

STATE_MARKER_PREFIX = "<!-- review-bot:state:"
STATE_MARKER_SUFFIX = " -->"


def state_marker(state_key: str) -> str:
    """Return the hidden HTML comment marker for a reviewed state key."""
    return f"{STATE_MARKER_PREFIX}{state_key}{STATE_MARKER_SUFFIX}"


def load_reviewed_states(state_file) -> set:
    """Read previously processed "owner/repo#PR:SHA" states from disk."""
    if not state_file.exists():
        log.warning(
            "State file %s not found (first run, or it was deleted); "
            "falling back to GitHub comment markers for deduplication.",
            state_file,
        )
        return set()
    try:
        return {
            line.strip()
            for line in state_file.read_text(encoding="utf-8").splitlines()
            if line.strip()
        }
    except OSError as exc:
        log.warning("Could not read state file %s: %s", state_file, exc)
        return set()


def mark_state_reviewed(state_file, state_key: str) -> None:
    """Append a processed state key to the local state file."""
    try:
        with state_file.open("a", encoding="utf-8") as handle:
            handle.write(state_key + "\n")
    except OSError as exc:
        log.error("Could not update state file %s: %s", state_file, exc)


def already_reviewed_on_github(pr, state_key: str, github_user: str) -> bool:
    """Check GitHub for a prior review of this exact PR+commit state.

    Durable fallback for when the local state file was deleted, modified,
    or lost: every review comment the bot posts embeds a hidden state
    marker, so scanning the PR's comments authored by the bot account tells
    us whether this commit SHA was already reviewed.
    """
    marker = state_marker(state_key)
    try:
        for comment in pr.get_issue_comments():
            if (
                comment.user
                and comment.user.login == github_user
                and marker in (comment.body or "")
            ):
                return True
    except GithubException as exc:
        # Fail open towards "not reviewed": a duplicate review is safer
        # than silently skipping a requested one.
        log.warning(
            "Could not scan existing comments for %s: %s", state_key, exc
        )
    return False
