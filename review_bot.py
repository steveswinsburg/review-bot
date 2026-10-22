#!/usr/bin/env python3
"""
Automated local code review bot.

Runs exactly once per invocation (designed for a 60-minute cron job), acting
under a personal GitHub account via a PAT. It finds open PRs in a target
organization where the user is a requested reviewer, checks the PR branch out
into a temporary git worktree inside the locally cloned repository, gathers
cross-repository architectural context from sibling clones in the workspace,
sends the combined payload to a locally hosted Ollama instance, and posts the
resulting review as a PR comment.

Required environment variables:
    GITHUB_TOKEN          Personal Access Token (repo scope).
    GITHUB_USER           The GitHub username the bot acts as.
    GITHUB_ORG            The organization to scan for review requests.
    LOCAL_WORKSPACE_PATH  Parent directory containing all org repos cloned
                          side-by-side (e.g. ~/workspace/org_name/).

Optional environment variables:
    OLLAMA_URL            Base URL of the Ollama instance
                          (default: http://localhost:11434).
    OLLAMA_MODEL          Model name to use (default: llama3).
"""

import json
import logging
import os
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

from github import Github, GithubException

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GITHUB_USER = os.environ.get("GITHUB_USER", "")
GITHUB_ORG = os.environ.get("GITHUB_ORG", "")
LOCAL_WORKSPACE_PATH = Path(
    os.path.expanduser(os.environ.get("LOCAL_WORKSPACE_PATH", ""))
)

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "llama3")

STATE_FILE = Path.home() / "reviewed_prs.txt"

# Hard cap on the combined payload (diff + cross-repo context) sent to the
# local LLM, protecting its context window.
MAX_PAYLOAD_CHARS = 30_000

# Filenames that carry systemic / architectural context in sibling repos.
CONTEXT_FILE_CANDIDATES = (
    "ARCHITECTURE.md",
    "architecture.md",
    "API.md",
    "api.md",
    "openapi.yaml",
    "openapi.yml",
    "openapi.json",
    "swagger.yaml",
    "swagger.json",
    "schema.graphql",
    "CONTRIBUTING.md",
    "README.md",
)

GIT_TIMEOUT_SECONDS = 120
OLLAMA_TIMEOUT_SECONDS = 600

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("review-bot")


# ---------------------------------------------------------------------------
# State management
# ---------------------------------------------------------------------------

def load_reviewed_states() -> set:
    """Read previously processed "owner/repo#PR:SHA" states from disk."""
    if not STATE_FILE.exists():
        return set()
    try:
        return {
            line.strip()
            for line in STATE_FILE.read_text(encoding="utf-8").splitlines()
            if line.strip()
        }
    except OSError as exc:
        log.warning("Could not read state file %s: %s", STATE_FILE, exc)
        return set()


def mark_state_reviewed(state_key: str) -> None:
    """Append a processed state key to the local state file."""
    try:
        with STATE_FILE.open("a", encoding="utf-8") as handle:
            handle.write(state_key + "\n")
    except OSError as exc:
        log.error("Could not update state file %s: %s", STATE_FILE, exc)


# ---------------------------------------------------------------------------
# Git helpers
# ---------------------------------------------------------------------------

def run_git(repo_path: Path, *args: str) -> str:
    """Run a git command inside repo_path and return stdout (raises on error)."""
    result = subprocess.run(
        ["git", "-C", str(repo_path), *args],
        capture_output=True,
        text=True,
        timeout=GIT_TIMEOUT_SECONDS,
        check=True,
    )
    return result.stdout


def detect_base_branch(repo_path: Path) -> str:
    """Return 'main' or 'master', whichever exists on origin."""
    for branch in ("main", "master"):
        probe = subprocess.run(
            ["git", "-C", str(repo_path), "rev-parse", "--verify",
             f"origin/{branch}"],
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_SECONDS,
        )
        if probe.returncode == 0:
            return branch
    return "main"


def collect_cross_repo_context(current_repo_name: str) -> str:
    """Gather architectural/API context from the *other* local repo clones.

    Reads well-known architectural files from the default branch working
    trees of sibling repositories inside LOCAL_WORKSPACE_PATH.
    """
    sections = []
    try:
        siblings = sorted(
            p for p in LOCAL_WORKSPACE_PATH.iterdir()
            if p.is_dir() and (p / ".git").exists() and p.name != current_repo_name
        )
    except OSError as exc:
        log.warning("Could not list workspace %s: %s", LOCAL_WORKSPACE_PATH, exc)
        return ""

    for sibling in siblings:
        for candidate in CONTEXT_FILE_CANDIDATES:
            candidate_path = sibling / candidate
            if candidate_path.is_file():
                try:
                    content = candidate_path.read_text(
                        encoding="utf-8", errors="replace"
                    )[:3_000]
                except OSError:
                    continue
                sections.append(
                    f"--- Context from sibling repo '{sibling.name}' "
                    f"({candidate}) ---\n{content}\n"
                )
    return "\n".join(sections)


# ---------------------------------------------------------------------------
# Ollama integration
# ---------------------------------------------------------------------------

REVIEW_PROMPT_TEMPLATE = """You are an elite Enterprise Software Architect \
with full visibility into the wider local codebase layout of this organization.

A pull request has been opened in repository '{repo_full_name}' (PR #{pr_number}: \
{pr_title}). Below you will find the raw git diff of the PR branch against the \
base branch, followed by architectural and API-definition context extracted \
from the neighboring, tightly-coupled repositories in the same workspace.

Your tasks:
1. Review the diff for correctness, security, performance, and maintainability.
2. Critically evaluate whether these changes will break contracts, schemas, \
APIs, or dependencies in the neighboring repositories whose context is provided.
3. Flag anything that violates the systemic architectural guidelines you can \
see in the provided context.

Output format requirements:
- Respond in clean, highly scannable markdown.
- Use concise bullet points grouped under short bold headings \
(e.g. **Cross-Repo Impact**, **Bugs & Risks**, **Suggestions**).
- Be practical and specific; reference file names and line context where possible.
- If everything looks good, say so briefly rather than inventing issues.

{payload}
"""


def request_ollama_review(prompt: str) -> str:
    """Send the prompt to the local Ollama instance and return the response."""
    body = json.dumps(
        {"model": OLLAMA_MODEL, "prompt": prompt, "stream": False}
    ).encode("utf-8")
    request = urllib.request.Request(
        f"{OLLAMA_URL.rstrip('/')}/api/generate",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=OLLAMA_TIMEOUT_SECONDS) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return (data.get("response") or "").strip()


# ---------------------------------------------------------------------------
# PR review workflow
# ---------------------------------------------------------------------------

def review_pr(pr, repo_full_name: str) -> bool:
    """Review a single PR using a temporary git worktree. Returns success."""
    repo_name = repo_full_name.split("/", 1)[1]
    repo_path = LOCAL_WORKSPACE_PATH / repo_name

    if not (repo_path / ".git").exists():
        log.error("Local clone missing for %s (expected at %s); skipping.",
                  repo_full_name, repo_path)
        return False

    head_sha = pr.head.sha
    pr_ref = f"pr-review-{pr.number}-{head_sha[:12]}"
    worktree_dir = Path(tempfile.gettempdir()) / f"review-bot-{repo_name}-{pr_ref}"
    worktree_created = False

    try:
        # Fetch the base branch and the PR head into a local ref.
        base_branch = detect_base_branch(repo_path)
        run_git(repo_path, "fetch", "origin", base_branch)
        run_git(repo_path, "fetch", "origin",
                f"pull/{pr.number}/head:{pr_ref}")

        # Create a detached temporary worktree for the PR head.
        run_git(repo_path, "worktree", "add", "--detach",
                str(worktree_dir), pr_ref)
        worktree_created = True

        # Read the raw diff locally from inside the worktree.
        diff_text = run_git(worktree_dir, "diff",
                            f"origin/{base_branch}...HEAD")
        if not diff_text.strip():
            log.info("PR %s#%d has an empty diff; skipping LLM call.",
                     repo_full_name, pr.number)
            return True

        # Cross-reference neighboring repositories for systemic context.
        cross_repo_context = collect_cross_repo_context(repo_name)

        payload = (
            "=== PR DIFF (git diff origin/"
            f"{base_branch}...HEAD) ===\n{diff_text}\n\n"
            "=== CROSS-REPOSITORY ARCHITECTURAL CONTEXT ===\n"
            f"{cross_repo_context}"
        )
        if len(payload) > MAX_PAYLOAD_CHARS:
            payload = payload[:MAX_PAYLOAD_CHARS] + "\n\n[payload truncated]"

        prompt = REVIEW_PROMPT_TEMPLATE.format(
            repo_full_name=repo_full_name,
            pr_number=pr.number,
            pr_title=pr.title,
            payload=payload,
        )

        review_text = request_ollama_review(prompt)
        if not review_text:
            log.error("Ollama returned an empty review for %s#%d.",
                      repo_full_name, pr.number)
            return False

        comment = (
            f"## 🤖 Automated Code Review (commit `{head_sha[:12]}`)\n\n"
            f"{review_text}"
        )
        pr.create_issue_comment(comment)
        log.info("Posted review on %s#%d.", repo_full_name, pr.number)
        return True

    except subprocess.TimeoutExpired as exc:
        log.error("Git command timed out for %s#%d: %s",
                  repo_full_name, pr.number, exc)
        return False
    except subprocess.CalledProcessError as exc:
        log.error("Git command failed for %s#%d: %s\nstderr: %s",
                  repo_full_name, pr.number, exc, exc.stderr)
        return False
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        log.error("Ollama unavailable or returned bad data for %s#%d: %s. "
                  "Will retry next cron cycle.", repo_full_name, pr.number, exc)
        return False
    except GithubException as exc:
        log.error("GitHub API error while reviewing %s#%d: %s",
                  repo_full_name, pr.number, exc)
        return False
    finally:
        # Cleanup guarantee: never leave dangling worktrees or refs behind.
        if worktree_created:
            subprocess.run(
                ["git", "-C", str(repo_path), "worktree", "remove",
                 "--force", str(worktree_dir)],
                capture_output=True,
                text=True,
                timeout=GIT_TIMEOUT_SECONDS,
            )
            subprocess.run(
                ["git", "-C", str(repo_path), "worktree", "prune"],
                capture_output=True,
                text=True,
                timeout=GIT_TIMEOUT_SECONDS,
            )
        subprocess.run(
            ["git", "-C", str(repo_path), "update-ref", "-d",
             f"refs/heads/{pr_ref}"],
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_SECONDS,
        )


# ---------------------------------------------------------------------------
# Main entry point (runs exactly once, then exits)
# ---------------------------------------------------------------------------

def main() -> int:
    missing = [
        name for name, value in (
            ("GITHUB_TOKEN", GITHUB_TOKEN),
            ("GITHUB_USER", GITHUB_USER),
            ("GITHUB_ORG", GITHUB_ORG),
            ("LOCAL_WORKSPACE_PATH", str(LOCAL_WORKSPACE_PATH)),
        ) if not value or value == "."
    ]
    if missing:
        log.error("Missing required environment variables: %s",
                  ", ".join(missing))
        return 1

    if not LOCAL_WORKSPACE_PATH.is_dir():
        log.error("LOCAL_WORKSPACE_PATH does not exist: %s",
                  LOCAL_WORKSPACE_PATH)
        return 1

    reviewed_states = load_reviewed_states()

    try:
        gh = Github(GITHUB_TOKEN)
        query = (
            f"is:open is:pr org:{GITHUB_ORG} "
            f"review-requested:{GITHUB_USER}"
        )
        issues = list(gh.search_issues(query))
    except GithubException as exc:
        log.error("GitHub search failed (will retry next cycle): %s", exc)
        return 1
    except Exception as exc:  # network drop, DNS failure, etc.
        log.error("Unexpected error reaching GitHub (will retry next "
                  "cycle): %s", exc)
        return 1

    log.info("Found %d open PR(s) requesting review from %s in org %s.",
             len(issues), GITHUB_USER, GITHUB_ORG)

    failures = 0
    for issue in issues:
        try:
            repo_full_name = issue.repository.full_name
            pr = issue.repository.get_pull(issue.number)
        except GithubException as exc:
            log.error("Could not load PR for issue %s: %s", issue, exc)
            failures += 1
            continue

        state_key = f"{repo_full_name}#{pr.number}:{pr.head.sha}"

        # Guard clause: never review my own PRs (prevents self-review loops).
        if pr.user.login == GITHUB_USER:
            if state_key not in reviewed_states:
                log.info("Skipping own PR %s#%d (authored by %s).",
                         repo_full_name, pr.number, GITHUB_USER)
                mark_state_reviewed(state_key)
                reviewed_states.add(state_key)
            continue

        if state_key in reviewed_states:
            log.info("Already reviewed %s; skipping.", state_key)
            continue

        log.info("Reviewing %s ...", state_key)
        if review_pr(pr, repo_full_name):
            mark_state_reviewed(state_key)
            reviewed_states.add(state_key)
        else:
            failures += 1

    log.info("Run complete. %d failure(s); exiting.", failures)
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
