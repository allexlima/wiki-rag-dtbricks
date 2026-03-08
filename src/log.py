"""
Structured logging configuration for the wiki-rag pipeline.

Usage:
    from src.log import get_logger
    log = get_logger(__name__)
"""
import logging
import sys

_CONFIGURED = False


def get_logger(name: str) -> logging.Logger:
    """Return a logger with consistent formatting. Configures root on first call."""
    global _CONFIGURED
    if not _CONFIGURED:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(
            logging.Formatter(
                fmt="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        )
        root = logging.getLogger("src")
        root.setLevel(logging.INFO)
        root.addHandler(handler)
        _CONFIGURED = True

    return logging.getLogger(name)
