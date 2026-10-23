#!/usr/bin/env python3
"""
Automated local code review bot.

Runs exactly once per invocation (designed for a 60-minute cron job), acting
under a personal GitHub account via a PAT. It finds open PRs in a target
organization or personal account where the user is a requested reviewer,
checks the PR branch out into a temporary git worktree inside the locally
cloned repository, gathers cross-repository architectural context from sibling
clones in the workspace, sends the combined payload to a configurable AI
provider (local Ollama by default, or remote providers such as the Cursor API,
OpenAI, or Anthropic via per-provider config files), and posts the resulting
review as a PR comment.

Required environment variables:
    GITHUB_TOKEN          Personal Access Token (repo scope).
    GITHUB_USER           The GitHub username the bot acts as.
    GITHUB_OWNER or
    GITHUB_ORG             The owner to scan (see configuration notes below).
    WORKSPACE_PATH       Parent directory containing repository clones
                          side-by-side for organization owners, or a single
                          repository checkout for personal-account owners.

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

AI provider configuration:
    Each provider is described by a small JSON file in the config directory,
    e.g. providers/ollama.json, providers/openai.json, providers/cursor.json,
    providers/anthropic.json. Supported "api_style" values:
        ollama     Ollama native /api/generate endpoint (no auth).
        openai     OpenAI-compatible /chat/completions endpoint (OpenAI,
                   Cursor, Azure OpenAI gateways, OpenRouter, vLLM, ...).
        anthropic  Anthropic /v1/messages endpoint.
    Separate configs can share an API style; cursor.json, for example, uses
    the OpenAI-compatible style.
    API keys are NEVER stored in the config files; each config names the
    environment variable holding the key via "api_key_env".
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

# Hard cap on the combined payload (diff + cross-repo context) sent to the
# LLM, protecting its context window (overridable per provider config).
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
AI_TIMEOUT_SECONDS = 600

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("review-bot")


# ---------------------------------------------------------------------------
# State management
# ---------------------------------------------------------------------------

# Hidden, machine-readable marker embedded in every review comment the bot
# posts. It makes GitHub itself a durable backup of the reviewed state, so a
# deleted or corrupted local state file never causes duplicate reviews.
STATE_MARKER_PREFIX = "<!-- review-bot:state:"
STATE_MARKER_SUFFIX = " -->"


def state_marker(state_key: str) -> str:
    """Return the hidden HTML comment marker for a reviewed state key."""
    return f"{STATE_MARKER_PREFIX}{state_key}{STATE_MARKER_SUFFIX}"


def load_reviewed_states() -> set:
    """Read previously processed "owner/repo#PR:SHA" states from disk."""
    if not STATE_FILE.exists():
        log.warning("State file %s not found (first run, or it was "
                    "deleted); falling back to GitHub comment markers for "
                    "deduplication.", STATE_FILE)
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


def already_reviewed_on_github(pr, state_key: str) -> bool:
    """Check GitHub for a prior review of this exact PR+commit state.

    Durable fallback for when the local state file was deleted, modified,
    or lost: every review comment the bot posts embeds a hidden state
    marker, so scanning the PR's comments authored by the bot account tells
    us whether this commit SHA was already reviewed.
    """
    marker = state_marker(state_key)
    try:
        for comment in pr.get_issue_comments():
            if (comment.user and comment.user.login == GITHUB_USER
                    and marker in (comment.body or "")):
                return True
    except GithubException as exc:
        # Fail open towards "not reviewed": a duplicate review is safer
        # than silently skipping a requested one.
        log.warning("Could not scan existing comments for %s: %s",
                    state_key, exc)
    return False


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


def list_workspace_repositories() -> list[Path]:
    """Return Git repositories checked out directly inside the workspace."""
    return sorted(
        path for path in WORKSPACE_PATH.iterdir()
        if path.is_dir() and (path / ".git").exists()
    )


def get_repositories_to_review(is_organization: bool) -> list[Path]:
    """Resolve review scope from an organization workspace or personal checkout."""
    if is_organization:
        return list_workspace_repositories()
    if (WORKSPACE_PATH / ".git").exists():
        return [WORKSPACE_PATH]
    raise ValueError(
        "For a personal GitHub account, WORKSPACE_PATH must point "
        "directly to the repository checkout to review."
    )


def is_workspace_repository(repository_name: str, workspace_repo_names: set[str]) -> bool:
    """Check whether a repository is included in the configured review scope."""
    return repository_name in workspace_repo_names


def collect_cross_repo_context(current_repo_name: str) -> str:
    """Gather architectural/API context from the *other* local repo clones.

    Reads well-known architectural files from the default branch working
    trees of sibling repositories inside WORKSPACE_PATH.
    """
    sections = []
    try:
        siblings = [
            path for path in list_workspace_repositories()
            if path.name != current_repo_name
        ]
    except OSError as exc:
        log.warning("Could not list workspace %s: %s", WORKSPACE_PATH, exc)
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
# AI provider integration (Ollama, OpenAI-compatible, Anthropic)
# ---------------------------------------------------------------------------

class AIProviderError(Exception):
    """Raised when a provider config is invalid or a provider call fails."""


class AIProvider:
    """An AI backend described by a per-provider JSON config file.

    Config schema (providers/<name>.json):
        {
          "api_style":   "ollama" | "openai" | "anthropic",   (required)
          "base_url":    "https://...",                       (required)
          "model":       "model-name",                        (required)
          "api_key_env": "ENV_VAR_HOLDING_KEY",               (required for
                                                               remote styles)
          "max_payload_chars": 30000,                         (optional)
          "timeout_seconds":   600,                           (optional)
          "max_tokens":        4096,                          (optional,
                                                               openai/anthropic)
          "extra_headers": {"Header-Name": "value"}           (optional)
        }

    API keys are never stored in config files; "api_key_env" names the
    environment variable that holds the secret.
    """

    SUPPORTED_API_STYLES = ("ollama", "openai", "anthropic")

    def __init__(self, name: str, config: dict):
        self.name = name
        self.api_style = config.get("api_style", "")
        self.base_url = str(config.get("base_url", "")).rstrip("/")
        self.model = config.get("model", "")
        self.max_payload_chars = int(
            config.get("max_payload_chars", MAX_PAYLOAD_CHARS)
        )
        self.timeout_seconds = int(
            config.get("timeout_seconds", AI_TIMEOUT_SECONDS)
        )
        self.max_tokens = int(config.get("max_tokens", 4096))
        self.extra_headers = dict(config.get("extra_headers", {}))

        if self.api_style not in self.SUPPORTED_API_STYLES:
            raise AIProviderError(
                f"Provider '{name}': unsupported api_style "
                f"'{self.api_style}' (expected one of "
                f"{', '.join(self.SUPPORTED_API_STYLES)})."
            )
        if not self.base_url or not self.model:
            raise AIProviderError(
                f"Provider '{name}': 'base_url' and 'model' are required."
            )

        self.api_key = ""
        api_key_env = config.get("api_key_env", "")
        if api_key_env:
            self.api_key = os.environ.get(api_key_env, "")
        if self.api_style != "ollama" and not self.api_key:
            raise AIProviderError(
                f"Provider '{name}': remote api_style '{self.api_style}' "
                f"requires an API key; set the '{api_key_env or 'api_key_env'}'"
                " environment variable."
            )

    # -- request/response shaping per API style ----------------------------

    def _build_request(self, prompt: str):
        headers = {"Content-Type": "application/json", **self.extra_headers}
        if self.api_style == "ollama":
            url = f"{self.base_url}/api/generate"
            payload = {"model": self.model, "prompt": prompt, "stream": False}
        elif self.api_style == "openai":
            url = f"{self.base_url}/chat/completions"
            headers["Authorization"] = "Bearer " + self.api_key
            payload = {
                "model": self.model,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": self.max_tokens,
                "stream": False,
            }
        else:  # anthropic
            url = f"{self.base_url}/v1/messages"
            headers["x-api-key"] = self.api_key
            headers.setdefault("anthropic-version", "2023-06-01")
            payload = {
                "model": self.model,
                "max_tokens": self.max_tokens,
                "messages": [{"role": "user", "content": prompt}],
            }
        return url, headers, payload

    def _parse_response(self, data: dict) -> str:
        if self.api_style == "ollama":
            return (data.get("response") or "").strip()
        if self.api_style == "openai":
            choices = data.get("choices") or []
            if choices:
                message = choices[0].get("message") or {}
                return (message.get("content") or "").strip()
            return ""
        # anthropic
        parts = data.get("content") or []
        return "\n".join(
            part.get("text", "") for part in parts
            if isinstance(part, dict) and part.get("type") == "text"
        ).strip()

    def request_review(self, prompt: str) -> str:
        """Send the prompt to the configured AI backend and return the text."""
        url, headers, payload = self._build_request(prompt)
        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(
            request, timeout=self.timeout_seconds
        ) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return self._parse_response(data)


def load_ai_provider(name: str) -> AIProvider:
    """Load the provider config file providers/<name>.json.

    The built-in 'ollama' provider works without a config file and honors the
    legacy OLLAMA_URL / OLLAMA_MODEL environment overrides.
    """
    config_path = AI_PROVIDER_CONFIG_DIR / f"{name}.json"
    if config_path.is_file():
        try:
            config = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise AIProviderError(
                f"Could not parse provider config {config_path}: {exc}"
            ) from exc
        # Allow legacy env overrides for the ollama provider.
        if name == "ollama":
            if os.environ.get("OLLAMA_URL"):
                config["base_url"] = os.environ["OLLAMA_URL"]
            if os.environ.get("OLLAMA_MODEL"):
                config["model"] = os.environ["OLLAMA_MODEL"]
        return AIProvider(name, config)

    if name == "ollama":
        return AIProvider(name, {
            "api_style": "ollama",
            "base_url": os.environ.get("OLLAMA_URL", "http://localhost:11434"),
            "model": os.environ.get("OLLAMA_MODEL", "llama3"),
        })

    raise AIProviderError(
        f"No config file found for AI provider '{name}' "
        f"(expected {config_path})."
    )


def owner_search_qualifier(
    gh: Github, owner: str, force_organization: bool = False
) -> str:
    """Return the GitHub search qualifier for an organization or user owner."""
    if force_organization:
        return "org"

    owner_type = gh.get_user(owner).type
    if owner_type == "Organization":
        return "org"
    if owner_type == "User":
        return "user"
    raise ValueError(
        f"GitHub owner '{owner}' has unsupported account type '{owner_type}'."
    )


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


# ---------------------------------------------------------------------------
# PR review workflow
# ---------------------------------------------------------------------------

def review_pr(
    pr,
    repo_full_name: str,
    provider: AIProvider,
    is_organization: bool = True,
) -> bool:
    """Review a single PR using a temporary git worktree. Returns success."""
    repo_name = repo_full_name.split("/", 1)[1]
    repo_path = (
        WORKSPACE_PATH / repo_name
        if is_organization else WORKSPACE_PATH
    )

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
        cross_repo_context = (
            collect_cross_repo_context(repo_name) if is_organization else ""
        )

        payload = (
            "=== PR DIFF (git diff origin/"
            f"{base_branch}...HEAD) ===\n{diff_text}\n\n"
            "=== CROSS-REPOSITORY ARCHITECTURAL CONTEXT ===\n"
            f"{cross_repo_context}"
        )
        if len(payload) > provider.max_payload_chars:
            payload = (payload[:provider.max_payload_chars]
                       + "\n\n[payload truncated]")

        prompt = REVIEW_PROMPT_TEMPLATE.format(
            repo_full_name=repo_full_name,
            pr_number=pr.number,
            pr_title=pr.title,
            payload=payload,
        )

        review_text = provider.request_review(prompt)
        if not review_text:
            log.error("AI provider '%s' returned an empty review for %s#%d.",
                      provider.name, repo_full_name, pr.number)
            return False

        comment = (
            f"{state_marker(f'{repo_full_name}#{pr.number}:{head_sha}')}\n"
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
        log.error("AI provider '%s' unavailable or returned bad data for "
                  "%s#%d: %s. Will retry next cron cycle.",
                  provider.name, repo_full_name, pr.number, exc)
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
        log.error("Missing required environment variables: %s",
                  ", ".join(missing))
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
        log.error("Unexpected error reaching GitHub (will retry next "
                  "cycle): %s", exc)
        return 1

    try:
        workspace_repositories = get_repositories_to_review(
            is_organization=qualifier == "org"
        )
    except OSError as exc:
        log.error("Could not list workspace %s: %s", WORKSPACE_PATH, exc)
        return 1
    except ValueError as exc:
        log.error("%s", exc)
        return 1
    workspace_repo_names = {path.name for path in workspace_repositories}
    if not workspace_repo_names:
        log.info("No Git repository clones found in %s; nothing to review.",
                 WORKSPACE_PATH)
        return 0

    reviewed_states = load_reviewed_states()

    try:
        provider = load_ai_provider(AI_PROVIDER)
    except AIProviderError as exc:
        log.error("AI provider configuration error: %s", exc)
        return 1
    log.info("Using AI provider '%s' (api_style=%s, model=%s).",
             provider.name, provider.api_style, provider.model)

    try:
        query = (
            f"is:open is:pr {qualifier}:{owner} "
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

    log.info(
        "Found %d open PR(s) requesting review from %s under %s %s; "
        "limiting reviews to %d checked-out repository/repositories.",
        len(issues), GITHUB_USER, "organization" if qualifier == "org"
        else "user", owner, len(workspace_repo_names),
    )

    failures = 0
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

        # Durable fallback: if the local state file was deleted or modified,
        # the hidden marker in previously posted review comments still tells
        # us this exact PR+commit was already reviewed. Self-heal the file.
        if already_reviewed_on_github(pr, state_key):
            log.info("Found existing review comment for %s on GitHub; "
                     "restoring state and skipping.", state_key)
            mark_state_reviewed(state_key)
            reviewed_states.add(state_key)
            continue

        log.info("Reviewing %s ...", state_key)
        if review_pr(
            pr, repo_full_name, provider, is_organization=qualifier == "org"
        ):
            mark_state_reviewed(state_key)
            reviewed_states.add(state_key)
        else:
            failures += 1

    log.info("Run complete. %d failure(s); exiting.", failures)
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
