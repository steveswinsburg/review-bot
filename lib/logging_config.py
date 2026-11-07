"""File logging under each workspace."""

import logging
import os
from pathlib import Path

from lib.config import DIGEST_DIR_NAME, LOG_FILE_NAME, LOG_FORMAT


def workspace_log_path(workspace_path: Path, override: str = "") -> Path:
    """Return the log file path for one workspace."""
    if override.strip():
        return Path(os.path.expanduser(override.strip()))
    return workspace_path / DIGEST_DIR_NAME / LOG_FILE_NAME


def attach_workspace_log_file(
    workspace_path: Path,
    override: str = "",
    logger_name: str = "review-bot",
) -> Path:
    """Append a log file handler for this workspace if one is not already attached."""
    log_path = workspace_log_path(workspace_path, override)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger(logger_name)
    logger.setLevel(logging.INFO)
    formatter = logging.Formatter(LOG_FORMAT)
    resolved = log_path.resolve()
    for handler in logger.handlers:
        if not isinstance(handler, logging.FileHandler):
            continue
        try:
            if Path(handler.baseFilename).resolve() == resolved:
                return log_path
        except OSError:
            continue

    handler = logging.FileHandler(log_path, encoding="utf-8")
    handler.setLevel(logging.INFO)
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    logger.info("Logging to %s", log_path)
    return log_path
