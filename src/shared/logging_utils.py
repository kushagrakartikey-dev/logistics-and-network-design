"""Consistent logger factory for every module in the portfolio."""
from __future__ import annotations

import logging
import os

_FORMAT = "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s"


def get_logger(name: str) -> logging.Logger:
    """Return a module logger with the portfolio-wide format.

    Level is controlled by the ``SCCC_LOG_LEVEL`` environment variable (default INFO).
    """
    logger = logging.getLogger(name)
    if not logging.getLogger().handlers and not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter(_FORMAT, "%H:%M:%S"))
        logger.addHandler(handler)
        logger.propagate = False
    logger.setLevel(os.environ.get("SCCC_LOG_LEVEL", "INFO"))
    return logger
