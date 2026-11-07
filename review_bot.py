#!/usr/bin/env python3
"""
Automated local code review bot.

Runs exactly once per invocation (designed for a 60-minute cron job), acting
under a personal GitHub account via a PAT. It finds open PRs in a target
organization or personal account where the user is a requested reviewer or
the author, checks the PR branch out into a temporary git worktree inside the
locally cloned repository, includes that repository's rules and digests of
sibling clones, reads other files only when the model asks, and posts the
resulting review as a PR comment.

Required environment variables:
    GITHUB_TOKEN          Personal Access Token (repo scope).
    GITHUB_USER           The GitHub username the bot acts as.
    GITHUB_OWNER or
    GITHUB_ORG             The owner to scan (see configuration notes below).
    WORKSPACE_PATH       Parent directory containing repository clones
                          side-by-side, or a single repository checkout.
                          Both layouts work for organizations and users.

Set either GITHUB_OWNER to automatically detect an organization or personal
account, or GITHUB_ORG when the owner is known to be an organization.

Optional environment variables:
    AI_PROVIDER           Name of the AI provider config to use (default:
                          "ollama"). Resolves to providers/<name>.json.
    AI_PROVIDER_CONFIG_DIR
                          Directory containing provider config files
                          (default: providers/ next to this script).
    OLLAMA_URL            Legacy override for the ollama provider's base URL
                          (default: http://localhost:11434).
    OLLAMA_MODEL          Legacy override for the ollama provider's model
                          (default: llama3).

The implementation lives in lib/. This module keeps the cron entry point and
the settings tests patch: workspace path, GitHub credentials, state file, and
the git and provider callables.
"""

import logging
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from github import Github, GithubException

from lib.provider import (
    AIProvider,
    AIProviderError,
    load_ai_provider as _load_ai_provider,
)
from lib.review import (
    assemble_review_payload,
    owner_search_qualifier,
    pull_request_search_query,
)
from lib.review import refresh_sibling_digests as _refresh_sibling_digests
from lib.review import review_pr as _review_pr
from lib.state import STATE_MARKER_PREFIX, STATE_MARKER_SUFFIX
from lib.state import already_reviewed_on_github as _already_reviewed_on_github
from lib.state import load_reviewed_states as _load_reviewed_states
from lib.state import mark_state_reviewed as _mark_state_reviewed
from lib.state import state_marker
from lib.workspace import detect_base_branch as _detect_base_branch
from lib.workspace import digest_is_fresh as _digest_is_fresh
from lib.workspace import is_workspace_repository
from lib.workspace import list_workspace_repositories as _list_workspace_repositories
from lib.workspace import read_digest as _read_digest
from lib.workspace import read_workspace_file as _read_workspace_file
from lib.workspace import run_git as _run_git
from lib.workspace import sibling_digests as _sibling_digests
from lib.workspace import sync_workspace_repositories as _sync_workspace_repositories
from lib.workspace import write_digest as _write_digest

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GITHUB_USER = os.environ.get("GITHUB_USER", "")
GITHUB_OWNER = os.environ.get("GITHUB_OWNER", "")
GITHUB_ORG = os.environ.get("GITHUB_ORG", "")
WORKSPACE_PATH = Path(
    os.path.expanduser(os.environ.get("WORKSPACE_PATH", ""))
)

AI_PROVIDER = os.environ.get("AI_PROVIDER", "ollama")
AI_PROVIDER_CONFIG_DIR = Path(
    os.path.expanduser(
        os.environ.get(
            "AI_PROVIDER_CONFIG_DIR",
            str(Path(__file__).resolve().parent / "providers"),
        )
    )
)

STATE_FILE = Path.home() / "reviewed_prs.txt"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("review-bot")

# Re-exported for callers and tests. The implementations live in lib/.
__all__ = [
    "AIProvider",
    "AIProviderError",
    "GITHUB_ORG",
    "GITHUB_OWNER",
    "GITHUB_TOKEN",
    "GITHUB_USER",
    "Github",
    "GithubException",
    "STATE_FILE",
    "STATE_MARKER_PREFIX",
    "STATE_MARKER_SUFFIX",
    "WORKSPACE_PATH",
    "already_reviewed_on_github",
    "assemble_review_payload",
    "detect_base_branch",
    "digest_is_fresh",
    "is_workspace_repository",
    "list_workspace_repositories",
    "load_ai_provider",
    "load_reviewed_states",
    "main",
    "mark_state_reviewed",
    "owner_search_qualifier",
    "pull_request_search_query",
    "read_digest",
    "read_workspace_file",
    "review_pr",
    "run_git",
    "sibling_digests",
    "state_marker",
    "sync_workspace_repositories",
    "write_digest",
]


def run_git(repo_path: Path, *args: str) -> str:
    """Run a git command. Tests replace this on the review_bot module."""
    return _run_git(repo_path, *args)


def detect_base_branch(repo_path: Path) -> str:
    """Return main or master. Tests replace this on the review_bot module."""
    return _detect_base_branch(repo_path)


def list_workspace_repositories() -> list[Path]:
    """Return the workspace checkout itself, or its direct child Git clones."""
    return _list_workspace_repositories(WORKSPACE_PATH)


def write_digest(repo_name: str, sha: str, body: str) -> None:
    """Write one repository digest beside the clones."""
    _write_digest(WORKSPACE_PATH, repo_name, sha, body)


def read_digest(repo_name: str):
    """Return (sha, body) for a repository digest, or None when unreadable."""
    return _read_digest(WORKSPACE_PATH, repo_name)


def digest_is_fresh(repo_path: Path) -> bool:
    """Return whether the stored digest matches the clone's current HEAD."""
    return _digest_is_fresh(WORKSPACE_PATH, repo_path, run_git)


def read_workspace_file(repo_name: str, relative_path: str, root: Path = None):
    """Read a file inside a workspace clone, or inside an explicit root."""
    return _read_workspace_file(WORKSPACE_PATH, repo_name, relative_path, root)


def sibling_digests(current_repo_name: str):
    """Return (repo name, digest body) for other clones in the workspace."""
    return _sibling_digests(WORKSPACE_PATH, current_repo_name)


def load_reviewed_states() -> set:
    """Read previously processed owner/repo#PR:SHA states from disk."""
    return _load_reviewed_states(STATE_FILE)


def mark_state_reviewed(state_key: str) -> None:
    """Append a processed state key to the local state file."""
    _mark_state_reviewed(STATE_FILE, state_key)


def already_reviewed_on_github(pr, state_key: str) -> bool:
    """Check GitHub for a prior review of this exact PR+commit state."""
    return _already_reviewed_on_github(pr, state_key, GITHUB_USER)


def load_ai_provider(name: str) -> AIProvider:
    """Load providers/<name>.json from the configured directory."""
    return _load_ai_provider(name, AI_PROVIDER_CONFIG_DIR)


def sync_workspace_repositories(repo_paths) -> int:
    """Fetch and reset workspace clones to origin's default branch."""
    return _sync_workspace_repositories(repo_paths, run_git, detect_base_branch)


def refresh_sibling_digests(repo_paths, repos_under_review, provider) -> None:
    """Rebuild stale digests for clones that provide context to this run."""
    _refresh_sibling_digests(
        repo_paths, repos_under_review, provider, WORKSPACE_PATH, run_git
    )


def review_pr(pr, repo_full_name: str, provider) -> bool:
    """Review a single PR using a temporary git worktree. Returns success."""
    return _review_pr(
        pr,
        repo_full_name,
        provider,
        workspace_path=WORKSPACE_PATH,
        run_git_command=run_git,
        detect_base_branch=detect_base_branch,
        subprocess_module=subprocess,
        tempfile_module=tempfile,
    )


def main() -> int:
    """Find pull requests, refresh stale digests, and post reviews once."""
    owner = GITHUB_OWNER or GITHUB_ORG
    force_organization = not GITHUB_OWNER and bool(GITHUB_ORG)
    missing = [
        name for name, value in (
            ("GITHUB_TOKEN", GITHUB_TOKEN),
            ("GITHUB_USER", GITHUB_USER),
            ("GITHUB_OWNER or GITHUB_ORG", owner),
            ("WORKSPACE_PATH", str(WORKSPACE_PATH)),
        ) if not value or value == "."
    ]
    if missing:
        log.error("Missing required environment variables: %s", ", ".join(missing))
        return 1

    if not WORKSPACE_PATH.is_dir():
        log.error("WORKSPACE_PATH does not exist: %s", WORKSPACE_PATH)
        return 1

    try:
        gh = Github(GITHUB_TOKEN)
        qualifier = owner_search_qualifier(gh, owner, force_organization)
    except GithubException as exc:
        log.error("Could not determine GitHub owner type: %s", exc)
        return 1
    except ValueError as exc:
        log.error("Could not determine GitHub owner type: %s", exc)
        return 1
    except Exception as exc:  # network drop, DNS failure, etc.
        log.error(
            "Unexpected error reaching GitHub (will retry next cycle): %s", exc
        )
        return 1

    try:
        workspace_repositories = list_workspace_repositories()
    except OSError as exc:
        log.error("Could not list workspace %s: %s", WORKSPACE_PATH, exc)
        return 1
    workspace_repo_names = {path.name for path in workspace_repositories}
    if not workspace_repo_names:
        log.info(
            "No Git repository clones found in %s; nothing to review.",
            WORKSPACE_PATH,
        )
        return 0

    reviewed_states = load_reviewed_states()

    try:
        provider = load_ai_provider(AI_PROVIDER)
    except AIProviderError as exc:
        log.error("AI provider configuration error: %s", exc)
        return 1
    log.info(
        "Using AI provider '%s' (api_style=%s, model=%s).",
        provider.name, provider.api_style, provider.model,
    )

    try:
        query = pull_request_search_query(qualifier, owner, GITHUB_USER)
        issues = list(gh.search_issues(query))
    except GithubException as exc:
        log.error("GitHub search failed (will retry next cycle): %s", exc)
        return 1
    except Exception as exc:  # network drop, DNS failure, etc.
        log.error(
            "Unexpected error reaching GitHub (will retry next cycle): %s", exc
        )
        return 1

    log.info(
        "Found %d open PR(s) requesting review from %s or authored by "
        "%s under %s %s; limiting reviews to %d checked-out "
        "repository/repositories.",
        len(issues), GITHUB_USER, GITHUB_USER,
        "organization" if qualifier == "org" else "user", owner,
        len(workspace_repo_names),
    )

    failures = 0
    pending = []
    for issue in issues:
        try:
            repository = issue.repository
            if not is_workspace_repository(repository.name, workspace_repo_names):
                continue
            repo_full_name = repository.full_name
            pr = repository.get_pull(issue.number)
        except GithubException as exc:
            log.error("Could not load PR for issue %s: %s", issue, exc)
            failures += 1
            continue

        state_key = f"{repo_full_name}#{pr.number}:{pr.head.sha}"

        if state_key in reviewed_states:
            log.info("Already reviewed %s; skipping.", state_key)
            continue

        if already_reviewed_on_github(pr, state_key):
            log.info(
                "Found existing review comment for %s on GitHub; "
                "restoring state and skipping.",
                state_key,
            )
            mark_state_reviewed(state_key)
            reviewed_states.add(state_key)
            continue

        pending.append((pr, repo_full_name, state_key))

    if pending:
        sync_failures = sync_workspace_repositories(workspace_repositories)
        if sync_failures:
            log.warning(
                "%d workspace clone(s) could not be synced to the default "
                "branch; sibling context may be stale.",
                sync_failures,
            )
        refresh_sibling_digests(
            workspace_repositories,
            {repo_full_name.split("/", 1)[1] for _pr, repo_full_name, _key in pending},
            provider,
        )

    for pr, repo_full_name, state_key in pending:
        log.info("Reviewing %s ...", state_key)
        if review_pr(pr, repo_full_name, provider):
            mark_state_reviewed(state_key)
            reviewed_states.add(state_key)
        else:
            failures += 1

    log.info("Run complete. %d failure(s); exiting.", failures)
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
