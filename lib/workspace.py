"""Workspace clones, digest files, and repository rule files."""

import logging
import subprocess
from pathlib import Path

from lib.config import (
    DIGEST_DIR_NAME,
    FILE_CLIP_CHARS,
    GIT_TIMEOUT_SECONDS,
    MAX_READ_PATHS,
    RULE_FILE_NAMES,
)

log = logging.getLogger("review-bot")


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


def list_workspace_repositories(workspace_path: Path) -> list[Path]:
    """Return the workspace checkout itself, or its direct child Git clones."""
    if (workspace_path / ".git").exists():
        return [workspace_path]
    return sorted(
        path for path in workspace_path.iterdir()
        if path.is_dir() and (path / ".git").exists()
    )


def is_workspace_repository(repository_name: str, workspace_repo_names: set[str]) -> bool:
    """Check whether a repository is included in the configured review scope."""
    return repository_name in workspace_repo_names


def digest_file(workspace_path: Path, repo_name: str) -> Path:
    """Return the workspace-root digest path for one repository."""
    return workspace_path / DIGEST_DIR_NAME / f"{repo_name}.md"


def write_digest(workspace_path: Path, repo_name: str, sha: str, body: str) -> None:
    """Write one repository digest beside the clones."""
    path = digest_file(workspace_path, repo_name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"sha: {sha}\n{body}", encoding="utf-8")


def read_digest(workspace_path: Path, repo_name: str):
    """Return (sha, body) for a repository digest, or None when unreadable."""
    path = digest_file(workspace_path, repo_name)
    if not path.is_file():
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        log.warning("Could not read digest %s: %s", path, exc)
        return None
    sha_line, newline, body = text.partition("\n")
    prefix = "sha: "
    if newline != "\n" or not sha_line.startswith(prefix):
        log.warning("Digest %s is missing its sha line.", path)
        return None
    return sha_line[len(prefix):].strip(), body


def digest_is_fresh(workspace_path: Path, repo_path: Path, run_git_command) -> bool:
    """Return whether the stored digest matches the clone's current HEAD."""
    stored = read_digest(workspace_path, repo_path.name)
    if stored is None:
        return False
    try:
        head = run_git_command(repo_path, "rev-parse", "HEAD").strip()
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        log.warning("Could not read HEAD for %s: %s", repo_path, exc)
        return False
    return stored[0] == head


def _safe_repo_name(repo_name: str) -> bool:
    return (
        bool(repo_name)
        and repo_name not in (".", "..")
        and "/" not in repo_name
        and "\\" not in repo_name
    )


def workspace_clone(workspace_path: Path, repo_name: str):
    """Return the workspace checkout for repo_name, or None."""
    if not _safe_repo_name(repo_name):
        return None
    if (workspace_path / ".git").exists():
        if workspace_path.name == repo_name:
            return workspace_path
        return None
    candidate = workspace_path / repo_name
    try:
        if candidate.resolve().parent != workspace_path.resolve():
            return None
    except OSError:
        return None
    if candidate.is_dir() and (candidate / ".git").exists():
        return candidate
    return None


def read_file_within(base: Path, relative_path: str):
    """Read a file that stays inside base, clipped to FILE_CLIP_CHARS."""
    if not relative_path or relative_path.startswith(("/", "\\")):
        return None
    try:
        base_resolved = base.resolve()
        candidate = (base / relative_path).resolve()
        candidate.relative_to(base_resolved)
    except (OSError, ValueError):
        return None
    if not candidate.is_file():
        return None
    try:
        return candidate.read_text(encoding="utf-8", errors="replace")[:FILE_CLIP_CHARS]
    except OSError as exc:
        log.warning("Could not read %s: %s", candidate, exc)
        return None


def read_workspace_file(
    workspace_path: Path, repo_name: str, relative_path: str, root: Path = None
):
    """Read a file inside a workspace clone, or inside an explicit root."""
    base = root if root is not None else workspace_clone(workspace_path, repo_name)
    if base is None:
        return None
    return read_file_within(base, relative_path)


def collect_rules(root: Path):
    """Return (label, text) rule files from a checkout, clipped per file."""
    if not root.is_dir():
        return []
    sections = []
    seen = set()

    def add(relative: str) -> None:
        try:
            resolved = (root / relative).resolve()
            resolved.relative_to(root.resolve())
        except (OSError, ValueError):
            return
        if resolved in seen:
            return
        text = read_file_within(root, relative)
        if not text:
            return
        seen.add(resolved)
        sections.append((relative, text))

    for name in RULE_FILE_NAMES:
        add(name)
    rules_dir = root / ".cursor" / "rules"
    if not rules_dir.is_dir():
        return sections
    for path in sorted(item for item in rules_dir.rglob("*") if item.is_file()):
        try:
            relative = path.resolve().relative_to(root.resolve()).as_posix()
        except ValueError:
            continue
        add(relative)
    return sections


def sibling_digests(workspace_path: Path, current_repo_name: str):
    """Return (repo name, digest body) for other clones in the workspace."""
    try:
        siblings = [
            path for path in list_workspace_repositories(workspace_path)
            if path.name != current_repo_name
        ]
    except OSError as exc:
        log.warning("Could not list workspace %s: %s", workspace_path, exc)
        return []
    sections = []
    for sibling in siblings:
        stored = read_digest(workspace_path, sibling.name)
        if stored is None:
            continue
        _sha, body = stored
        if body.strip():
            sections.append((sibling.name, body))
    return sections


def labeled_block(pairs) -> str:
    """Format labeled text sections for a model prompt."""
    return "\n".join(f"--- {label} ---\n{text}" for label, text in pairs)


def build_digest_prompt(repo_path: Path, sha: str, max_chars: int, run_git_command) -> str:
    """Ask the configured model for one repository digest."""
    try:
        file_list = run_git_command(repo_path, "ls-files")
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        log.warning("Could not list files in %s: %s", repo_path, exc)
        file_list = ""
    rules = labeled_block(collect_rules(repo_path))
    intro = (
        f"Document the repository '{repo_path.name}' at commit {sha}.\n"
        "Write a digest of its exports, routes, and types. "
        "Be specific and concise. Do not ask questions. Do not request files. "
        "Reply with the digest only.\n\n"
    )
    rules_section = f"Rules:\n{rules}\n\n" if rules else ""
    files_header = "File list:\n"
    budget = max_chars - len(intro) - len(rules_section) - len(files_header)
    if budget < 0:
        rules_section = rules_section[: max(0, max_chars - len(intro))]
        budget = max_chars - len(intro) - len(rules_section) - len(files_header)
    if budget < 0:
        budget = 0
    return intro + rules_section + files_header + file_list[:budget]


def read_requested_files(workspace_path: Path, paths, repo_name: str, worktree: Path):
    """Read up to MAX_READ_PATHS files named as repo/relative-path."""
    sections = []
    for spec in paths[:MAX_READ_PATHS]:
        if not isinstance(spec, str) or "/" not in spec:
            continue
        name, relative = spec.split("/", 1)
        if not _safe_repo_name(name) or not relative:
            continue
        if name == repo_name:
            root = worktree
        else:
            root = workspace_clone(workspace_path, name)
            if root is None:
                continue
        text = read_file_within(root, relative)
        if text is None:
            continue
        sections.append((spec, text))
    return sections
