"""Local stderr logging for the tools package."""

from __future__ import annotations

import logging
import sys


LOGGER_NAME = "persistent_agent_runtime.tools"


def get_tools_logger() -> logging.Logger:
    """Return a stderr logger for MCP tool runtime events."""
    logger = logging.getLogger(LOGGER_NAME)
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
        )
        logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    return logger
