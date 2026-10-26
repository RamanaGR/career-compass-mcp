"""Career Compass MCP: Privacy-first Interactive Web Form Accessibility & Prefill Assistant."""

from __future__ import annotations

import logging
import sys

__version__ = "0.1.0"


def configure_stderr_logging(level: int = logging.INFO) -> logging.Logger:
    """Configure package logging strictly to sys.stderr to keep MCP stdio clean."""
    logger = logging.getLogger("compass_mcp")
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stderr)
        formatter = logging.Formatter(
            fmt="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        handler.setFormatter(formatter)
        logger.addHandler(handler)
    logger.setLevel(level)
    logger.propagate = False
    return logger


configure_stderr_logging()
