from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable


class CallbackHandler(logging.Handler):
    def __init__(self, callback: Callable[[str], None]):
        super().__init__()
        self.callback = callback

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.callback(self.format(record))
        except Exception:
            self.handleError(record)


def create_logger(base_dir: Path, callback: Callable[[str], None] | None = None) -> logging.Logger:
    logs = base_dir / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(f"heybox_exporter.{id(callback)}")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    logger.propagate = False
    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S")
    file_handler = logging.FileHandler(logs / "latest.log", mode="w", encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    if callback:
        callback_handler = CallbackHandler(callback)
        callback_handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(callback_handler)
    return logger

