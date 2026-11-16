"""Logging: a rotating file for everything, plus a hook the screen's Log panel listens to."""
from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Callable

FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"


def setup_logging(path: str | Path, level: int = logging.INFO) -> logging.Logger:
    log_path = Path(path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("stock_terminal")
    logger.setLevel(level)
    if not any(isinstance(h, RotatingFileHandler) for h in logger.handlers):
        handler = RotatingFileHandler(log_path, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
        handler.setFormatter(logging.Formatter(FORMAT))
        logger.addHandler(handler)
    # Third-party chatter stays out of the file unless it is a warning or worse.
    for noisy in ("httpx", "httpcore", "urllib3", "websockets"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    return logger


class CallbackHandler(logging.Handler):
    """Hands each formatted log line to a callback (the screen's Log panel)."""

    def __init__(self, callback: Callable[[str], None], level: int = logging.WARNING) -> None:
        super().__init__(level)
        self.callback = callback
        self.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S"))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.callback(self.format(record))
        except Exception:  # noqa: BLE001  a broken log panel must never take the app down
            self.handleError(record)
