"""Prompt assembly, digest refresh, and the unattended review loop."""

import json
import logging
import subprocess
import urllib.error
from pathlib import Path

from github import Github, GithubException

from lib.config import GIT_TIMEOUT_SECONDS, MAX_MODEL_CALLS
from lib.provider import AIProviderError
from lib.state import state_marker
from lib.workspace import (
    build_digest_prompt,
    collect_rules,
    digest_is_fresh,
    labeled_block,
    read_requested_files,
    sibling_digests,
    write_digest,
)

log = logging.getLogger("review-bot")

REVIEW_PROMPT_TEMPLATE = """You are an elite Enterprise Software Architect \
with full visibility into the wider local codebase layout of this organization.

A pull request has been opened in repository '{repo_full_name}' (PR #{pr_number}: \
{pr_title}). Below you will find the raw git diff of the PR branch against the \
base branch, the rules from that repository, and digests of the other \
repositories in this workspace.

Your tasks:
1. Review the diff for correctness, security, performance, and maintainability.
2. Critically evaluate whether these changes will break contracts, schemas, \
APIs, or dependencies in the neighboring repositories whose digests are provided.
3. Flag anything that violates the rules from the repository under review.

Output format requirements:
- Respond in clean, highly scannable markdown.
- Use concise bullet points grouped under short bold headings \
(e.g. **Cross-Repo Impact**, **Bugs & Risks**, **Suggestions**).
- Be practical and specific; reference file names and line context where possible.
- If everything looks good, say so briefly rather than inventing issues.
- This review is posted as a one-way GitHub comment. The author cannot reply \
to you, and you cannot implement follow-up changes.
- Do not ask questions, request confirmation, or offer to do more work.
- Do not end with invitations or closings such as "if you like", "let me know", \
"happy to", "I can also", "we can", or "would you like".
- State findings and concrete recommended changes directly, then stop.
- To read files before finishing, reply with only a JSON object of this form \
and no other text: {{"read":["repo-name/relative/path"]}}
- Request at most 8 paths. Each path is a workspace repository name, a slash, \
and the path inside that repository.
- Any other reply is posted as the review. A question is not posted.

{payload}
"""


def pull_request_search_query(qualifier: str, owner: str, user: str) -> str:
    """Return the GitHub search query for PRs this bot should consider.

    Includes PRs that request ``user`` as a reviewer *and* PRs that ``user``
    authored. GitHub will not let a pull request author request themselves as
    an individual reviewer, so authored PRs must be selected explicitly.
    Parentheses keep ``author:`` scoped to the same owner as the rest of the
    query.
    """
    return (
        f"is:open is:pr {qualifier}:{owner} "
        f"(review-requested:{user} OR author:{user})"
    )


def owner_search_qualifier(gh: Github, owner: str, force_organization: bool = False) -> str:
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


def assemble_review_payload(
    base_branch, diff_text, rules, digests, files, max_chars
) -> str:
    """Build the review payload, dropping context before cutting the diff."""
    header = f"=== PR DIFF (git diff origin/{base_branch}...HEAD) ===\n"

    def render(diff, rule_pairs, digest_pairs, file_pairs) -> str:
        parts = [header + diff]
        if rule_pairs:
            parts.append("=== REPOSITORY RULES ===\n" + labeled_block(rule_pairs))
        if digest_pairs:
            parts.append(
                "=== SIBLING REPOSITORY DIGESTS ===\n" + labeled_block(digest_pairs)
            )
        if file_pairs:
            parts.append("=== REQUESTED FILES ===\n" + labeled_block(file_pairs))
        return "\n\n".join(parts)

    for rule_pairs, digest_pairs, file_pairs in (
        (rules, digests, files),
        (rules, digests, []),
        (rules, [], []),
        ([], [], []),
    ):
        payload = render(diff_text, rule_pairs, digest_pairs, file_pairs)
        if len(payload) <= max_chars:
            return payload

    marker = "\n\n[diff truncated]"
    keep = max_chars - len(header) - len(marker)
    if keep < 0:
        keep = 0
    payload = header + diff_text[:keep] + marker
    if len(payload) > max_chars:
        payload = payload[: max_chars - len(marker)] + marker
    return payload


def strip_model_wrapper(text: str) -> str:
    """Remove one leading think block and one surrounding code fence."""
    cleaned = (text or "").strip()
    if cleaned.startswith("<think>"):
        end = cleaned.find("</think>")
        if end != -1:
            cleaned = cleaned[end + len("</think>"):].strip()
    if cleaned.startswith("```"):
        lines = cleaned.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        cleaned = "\n".join(lines).strip()
    return cleaned


def classify_model_reply(text: str):
    """Return ('read', paths), ('review', text), or ('stop', reason)."""
    cleaned = strip_model_wrapper(text)
    if not cleaned:
        return "stop", "empty"
    if cleaned.startswith("{") and cleaned.endswith("}"):
        try:
            data = json.loads(cleaned)
        except json.JSONDecodeError:
            return "stop", "invalid"
        reads = data.get("read") if isinstance(data, dict) else None
        if (
            isinstance(data, dict)
            and set(data) == {"read"}
            and isinstance(reads, list)
            and all(isinstance(item, str) for item in reads)
        ):
            return "read", reads
        return "stop", "invalid"
    lines = [line.strip() for line in cleaned.splitlines() if line.strip()]
    if lines and all(line.endswith("?") for line in lines):
        return "stop", "question"
    return "review", cleaned


def refresh_sibling_digests(
    repo_paths, repos_under_review, provider, workspace_path: Path, run_git_command
) -> None:
    """Rebuild stale digests for clones that provide context to this run."""
    if len(repo_paths) <= 1 or not repos_under_review:
        return
    for repo_path in repo_paths:
        if not any(name != repo_path.name for name in repos_under_review):
            continue
        if digest_is_fresh(workspace_path, repo_path, run_git_command):
            log.info("Digest for %s is current.", repo_path.name)
            continue
        try:
            sha = run_git_command(repo_path, "rev-parse", "HEAD").strip()
            prompt = build_digest_prompt(
                repo_path, sha, provider.max_payload_chars, run_git_command
            )
            reply = provider.request_review(prompt)
        except (
            OSError,
            subprocess.CalledProcessError,
            subprocess.TimeoutExpired,
            urllib.error.URLError,
            TimeoutError,
            json.JSONDecodeError,
            AIProviderError,
        ) as exc:
            log.error("Could not digest %s: %s", repo_path.name, exc)
            continue
        kind, value = classify_model_reply(reply)
        if kind != "review":
            log.error(
                "Could not digest %s: model returned %s.", repo_path.name, value
            )
            continue
        body = value if value.endswith("\n") else value + "\n"
        write_digest(workspace_path, repo_path.name, sha, body)
        log.info("Wrote digest for %s at %s.", repo_path.name, sha)


def review_pr(
    pr,
    repo_full_name: str,
    provider,
    workspace_path: Path,
    run_git_command,
    detect_base_branch,
    subprocess_module,
    tempfile_module,
) -> bool:
    """Review a single PR using a temporary git worktree. Returns success."""
    repo_name = repo_full_name.split("/", 1)[1]
    repo_path = (
        workspace_path if (workspace_path / ".git").exists()
        else workspace_path / repo_name
    )

    if not (repo_path / ".git").exists():
        log.error(
            "Local clone missing for %s (expected at %s); skipping.",
            repo_full_name, repo_path,
        )
        return False

    head_sha = pr.head.sha
    pr_ref = f"pr-review-{pr.number}-{head_sha[:12]}"
    worktree_dir = (
        Path(tempfile_module.gettempdir()) / f"review-bot-{repo_name}-{pr_ref}"
    )
    worktree_created = False

    try:
        base_branch = detect_base_branch(repo_path)
        run_git_command(repo_path, "fetch", "origin", base_branch)
        run_git_command(
            repo_path, "fetch", "origin", f"pull/{pr.number}/head:{pr_ref}"
        )

        run_git_command(
            repo_path, "worktree", "add", "--detach", str(worktree_dir), pr_ref
        )
        worktree_created = True

        diff_text = run_git_command(
            worktree_dir, "diff", f"origin/{base_branch}...HEAD"
        )
        if not diff_text.strip():
            log.info(
                "PR %s#%d has an empty diff; skipping LLM call.",
                repo_full_name, pr.number,
            )
            return True

        rules = collect_rules(worktree_dir)
        digests = sibling_digests(workspace_path, repo_name)
        requested = []
        review_text = None
        for _attempt in range(MAX_MODEL_CALLS):
            payload = assemble_review_payload(
                base_branch=base_branch,
                diff_text=diff_text,
                rules=rules,
                digests=digests,
                files=requested,
                max_chars=provider.max_payload_chars,
            )
            prompt = REVIEW_PROMPT_TEMPLATE.format(
                repo_full_name=repo_full_name,
                pr_number=pr.number,
                pr_title=pr.title,
                payload=payload,
            )
            reply = provider.request_review(prompt)
            kind, value = classify_model_reply(reply)
            if kind == "read":
                requested = read_requested_files(
                    workspace_path, value, repo_name, worktree_dir
                )
                continue
            if kind == "review":
                review_text = value
                break
            log.error(
                "AI provider '%s' returned %s instead of a review for %s#%d.",
                provider.name, value, repo_full_name, pr.number,
            )
            return False
        else:
            log.error(
                "AI provider '%s' did not settle on a review for %s#%d "
                "after %d calls.",
                provider.name, repo_full_name, pr.number, MAX_MODEL_CALLS,
            )
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
        log.error(
            "Git command timed out for %s#%d: %s",
            repo_full_name, pr.number, exc,
        )
        return False
    except subprocess.CalledProcessError as exc:
        log.error(
            "Git command failed for %s#%d: %s\nstderr: %s",
            repo_full_name, pr.number, exc, exc.stderr,
        )
        return False
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        log.error(
            "AI provider '%s' unavailable or returned bad data for "
            "%s#%d: %s. Will retry next cron cycle.",
            provider.name, repo_full_name, pr.number, exc,
        )
        return False
    except GithubException as exc:
        log.error(
            "GitHub API error while reviewing %s#%d: %s",
            repo_full_name, pr.number, exc,
        )
        return False
    finally:
        if worktree_created:
            subprocess_module.run(
                ["git", "-C", str(repo_path), "worktree", "remove",
                 "--force", str(worktree_dir)],
                capture_output=True,
                text=True,
                timeout=GIT_TIMEOUT_SECONDS,
            )
            subprocess_module.run(
                ["git", "-C", str(repo_path), "worktree", "prune"],
                capture_output=True,
                text=True,
                timeout=GIT_TIMEOUT_SECONDS,
            )
        subprocess_module.run(
            ["git", "-C", str(repo_path), "update-ref", "-d",
             f"refs/heads/{pr_ref}"],
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_SECONDS,
        )
