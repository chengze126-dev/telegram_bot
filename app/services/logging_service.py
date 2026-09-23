"""Application logging: rotating log file + an in-memory feed for the Logs screen."""

from __future__ import annotations

import logging
import threading
from collections import deque
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

from PySide6.QtCore import QObject, Signal

MAX_MEMORY_RECORDS = 5000

_SOURCE_NAMES = {
    "app.scanner": "Scanner",
    "app.worker": "Worker",
    "app.ingest": "Storage",
    "app.avatars": "Avatars",
    "app.notifications": "Notifications",
    "app.database": "Database",
    "app.config": "Config",
    "app.ui": "UI",
}


def _source(name: str) -> str:
    if name.startswith("app.telegram"):
        return "Telegram"
    if name.startswith("telethon"):
        return "Telethon"
    return _SOURCE_NAMES.get(name, name.split(".")[-1].title())


class LogBus(QObject):
    record = Signal(dict)


class QtLogHandler(logging.Handler):
    """Keeps recent records in memory and forwards new ones to the UI thread."""

    def __init__(self) -> None:
        super().__init__(level=logging.INFO)
        self.bus = LogBus()
        self.history: deque[dict] = deque(maxlen=MAX_MEMORY_RECORDS)
        self._lock_hist = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = record.getMessage()
            if record.exc_info and record.levelno >= logging.ERROR:
                message = f"{message} ({record.exc_info[1]!r})"
            entry = {
                "time": datetime.fromtimestamp(record.created),
                "level": record.levelname,
                "source": _source(record.name),
                "message": message,
            }
            with self._lock_hist:
                self.history.append(entry)
            self.bus.record.emit(entry)
        except Exception:
            self.handleError(record)


class _TelethonFilter(logging.Filter):
    """Pass Telethon warnings/errors plus its flood-wait and reconnect notices."""

    KEYWORDS = ("flood wait", "reconnect", "disconnect", "connection")

    def filter(self, record: logging.LogRecord) -> bool:
        if not record.name.startswith("telethon"):
            return True
        if record.levelno >= logging.WARNING:
            return True
        msg = record.getMessage().lower()
        return any(k in msg for k in self.KEYWORDS)


_qt_handler: QtLogHandler | None = None


def setup_logging(log_file: Path, debug: bool = False) -> QtLogHandler:
    global _qt_handler
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if debug else logging.INFO)
    for h in list(root.handlers):
        root.removeHandler(h)

    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    file_handler = RotatingFileHandler(log_file, maxBytes=2_000_000, backupCount=5, encoding="utf-8")
    file_handler.setFormatter(fmt)
    file_handler.addFilter(_TelethonFilter())
    root.addHandler(file_handler)

    console = logging.StreamHandler()
    console.setFormatter(fmt)
    console.setLevel(logging.DEBUG if debug else logging.WARNING)
    root.addHandler(console)

    _qt_handler = QtLogHandler()
    _qt_handler.setLevel(logging.DEBUG if debug else logging.INFO)
    _qt_handler.addFilter(_TelethonFilter())
    root.addHandler(_qt_handler)

    logging.getLogger("telethon").setLevel(logging.INFO)
    return _qt_handler


def qt_handler() -> QtLogHandler | None:
    return _qt_handler
