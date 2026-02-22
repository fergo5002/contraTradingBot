"""
Structured logging: rotating file handler + Rich console handler.
All project modules call get_logger(__name__) to obtain a child logger
that inherits handlers from the root "contra_bot" logger.
"""

import logging
import logging.handlers
from pathlib import Path

from rich.logging import RichHandler

_initialized = False


def _setup_root_logger() -> None:
    global _initialized
    if _initialized:
        return
    _initialized = True

    Path("logs").mkdir(exist_ok=True)

    root = logging.getLogger("contra_bot")
    root.setLevel(logging.DEBUG)

    # ── Rotating file handler (keeps full DEBUG-level detail) ──────────────
    fh = logging.handlers.RotatingFileHandler(
        "logs/contra_bot.log",
        maxBytes=10 * 1024 * 1024,  # 10 MB per file
        backupCount=5,
        encoding="utf-8",
    )
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(
        logging.Formatter(
            "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )

    # ── Rich console handler (INFO and above, human-readable) ──────────────
    ch = RichHandler(
        show_time=True,
        show_path=False,
        rich_tracebacks=True,
        tracebacks_show_locals=False,
        markup=True,
    )
    ch.setLevel(logging.INFO)

    root.addHandler(fh)
    root.addHandler(ch)


def get_logger(name: str) -> logging.Logger:
    """
    Return a child logger of the contra_bot root.
    Usage in every module:
        from logger import get_logger
        logger = get_logger(__name__)
    """
    _setup_root_logger()
    # Use only the leaf module name so log lines stay concise.
    leaf = name.split(".")[-1] if "." in name else name
    return logging.getLogger(f"contra_bot.{leaf}")
