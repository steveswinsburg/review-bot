"""Shared limits for review payloads, digests, and git calls."""

MAX_PAYLOAD_CHARS = 30_000
GIT_TIMEOUT_SECONDS = 120
AI_TIMEOUT_SECONDS = 600
FILE_CLIP_CHARS = 4_000
MAX_READ_PATHS = 8
MAX_MODEL_CALLS = 12
DIGEST_DIR_NAME = ".review-bot"

RULE_FILE_NAMES = (
    "AGENTS.md",
    "CLAUDE.md",
    "CONTRIBUTING.md",
    "ARCHITECTURE.md",
    "architecture.md",
)
