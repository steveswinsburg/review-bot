"""Tests for per-workspace log files."""

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.logging_config import attach_workspace_log_file, workspace_log_path  # noqa: E402


def _reset_review_bot_logger() -> None:
    logging.getLogger("review-bot").handlers.clear()


def test_workspace_log_path_default(tmp_path):
    workspace = tmp_path / "ws"
    assert workspace_log_path(workspace) == workspace / ".review-bot" / "review-bot.log"


def test_workspace_log_file_receives_messages(tmp_path):
    _reset_review_bot_logger()
    logger = logging.getLogger("review-bot")
    workspace = tmp_path / "ws"
    log_path = attach_workspace_log_file(workspace)
    logger.info("run complete")
    for handler in logger.handlers:
        if isinstance(handler, logging.FileHandler):
            handler.flush()
    assert "run complete" in log_path.read_text(encoding="utf-8")
