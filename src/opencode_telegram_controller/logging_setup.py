"""Logging configuration using loguru."""

from __future__ import annotations

import sys

from loguru import logger

from .config import Settings


def setup_logging(settings: Settings) -> None:
    logger.remove()
    logger.add(sys.stderr, level=settings.log_level)
    if settings.log_file:
        settings.log_file.parent.mkdir(parents=True, exist_ok=True)
        logger.add(
            settings.log_file,
            level="DEBUG",
            rotation="10 MB",
            retention=7,
            encoding="utf-8",
        )
