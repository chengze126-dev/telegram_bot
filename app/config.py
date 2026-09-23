"""Application configuration: paths, credentials and user-editable settings.

Credentials (API ID / hash / phone / bot token) are read from environment
variables or the project's ``.env`` file and are never hard-coded. Non-secret
settings (polling interval, thresholds, UI behaviour) live in ``settings.json``
inside the per-user data directory.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
from dataclasses import asdict, dataclass, field, fields
from datetime import date
from pathlib import Path

from dotenv import load_dotenv, set_key

log = logging.getLogger("app.config")

APP_NAME = "TG Lead Monitor"
APP_ID = "telegram-lead-monitor"
APP_VERSION = "1.0.0"

PROJECT_ROOT = Path(__file__).resolve().parent.parent
RESOURCES_DIR = Path(__file__).resolve().parent / "resources"
ENV_PATH = Path(os.environ.get("TLM_ENV_FILE", PROJECT_ROOT / ".env"))

load_dotenv(ENV_PATH, override=False)


def _default_data_dir() -> Path:
    override = os.environ.get("TLM_DATA_DIR")
    if override:
        return Path(override).expanduser()
    if sys.platform.startswith("win"):
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
        return base / "TelegramLeadMonitor"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "TelegramLeadMonitor"
    base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return base / APP_ID


def _secure_mkdir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    if os.name == "posix":
        try:
            os.chmod(path, 0o700)
        except OSError:
            pass
    return path


class Paths:
    """All on-disk locations used by the application."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = _secure_mkdir(root or _default_data_dir())
        self.sessions = _secure_mkdir(self.root / "sessions")
        self.avatars = _secure_mkdir(self.root / "avatars")
        self.logs = _secure_mkdir(self.root / "logs")
        self.exports = _secure_mkdir(self.root / "exports")
        self.database = self.root / "monitor.db"
        self.settings = self.root / "settings.json"
        self.session_file = self.sessions / "telegram"  # Telethon appends ".session"
        self.log_file = self.logs / "monitor.log"


# --------------------------------------------------------------------------- #
# Credentials
# --------------------------------------------------------------------------- #


@dataclass
class Credentials:
    api_id: int | None
    api_hash: str
    phone: str
    bot_token: str

    @classmethod
    def load(cls) -> "Credentials":
        raw_id = os.environ.get("TELEGRAM_API_ID", "").strip()
        try:
            api_id = int(raw_id) if raw_id else None
        except ValueError:
            log.error("TELEGRAM_API_ID must be a number; ignoring invalid value")
            api_id = None
        return cls(
            api_id=api_id,
            api_hash=os.environ.get("TELEGRAM_API_HASH", "").strip(),
            phone=os.environ.get("TELEGRAM_PHONE", "").strip(),
            bot_token=os.environ.get("TELEGRAM_BOT_TOKEN", "").strip(),
        )

    @property
    def complete(self) -> bool:
        return bool(self.api_id and self.api_hash)

    def save(self) -> None:
        """Persist credentials to the project's .env file (created with mode 600)."""
        if not ENV_PATH.exists():
            ENV_PATH.touch()
        if os.name == "posix":
            try:
                os.chmod(ENV_PATH, 0o600)
            except OSError:
                pass
        values = {
            "TELEGRAM_API_ID": str(self.api_id or ""),
            "TELEGRAM_API_HASH": self.api_hash,
            "TELEGRAM_PHONE": self.phone,
            "TELEGRAM_BOT_TOKEN": self.bot_token,
        }
        for key, value in values.items():
            set_key(str(ENV_PATH), key, value, quote_mode="never")
            os.environ[key] = value


def mask_secret(value: str, visible: int = 4) -> str:
    if not value:
        return ""
    if len(value) <= visible:
        return "•" * len(value)
    return "•" * (len(value) - visible) + value[-visible:]


# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _default_joined_before() -> str:
    raw = os.environ.get("JOINED_BEFORE", "2025-01-01").strip()
    try:
        return date.fromisoformat(raw).isoformat()
    except ValueError:
        return "2025-01-01"


@dataclass
class AppSettings:
    scan_interval_seconds: int = field(default_factory=lambda: _env_int("SCAN_INTERVAL_SECONDS", 60))
    member_sync_minutes: int = field(default_factory=lambda: _env_int("MEMBER_SYNC_MINUTES", 30))
    history_backfill: int = field(default_factory=lambda: _env_int("HISTORY_BACKFILL", 300))
    max_messages_per_scan: int = 1000
    max_members_per_sync: int = 10000
    avatar_downloads_per_cycle: int = 25
    api_min_interval: float = 0.35
    min_relevance: int = field(default_factory=lambda: _env_int("MIN_RELEVANCE_SCORE", 50))
    joined_before: str = field(default_factory=_default_joined_before)
    notifications_enabled: bool = field(default_factory=lambda: _env_bool("NOTIFICATIONS_ENABLED", True))
    start_scanning_on_launch: bool = True
    start_minimized: bool = False
    close_to_tray: bool = True

    LIMITS = {
        "scan_interval_seconds": (15, 3600),
        "member_sync_minutes": (5, 1440),
        "history_backfill": (0, 5000),
        "max_messages_per_scan": (50, 5000),
        "max_members_per_sync": (100, 10000),
        "avatar_downloads_per_cycle": (0, 200),
        "min_relevance": (0, 100),
    }

    def normalized(self) -> "AppSettings":
        for name, (lo, hi) in self.LIMITS.items():
            setattr(self, name, max(lo, min(hi, int(getattr(self, name)))))
        self.api_min_interval = max(0.1, min(5.0, float(self.api_min_interval)))
        try:
            self.joined_before = date.fromisoformat(str(self.joined_before)).isoformat()
        except ValueError:
            self.joined_before = "2025-01-01"
        return self

    @property
    def joined_before_date(self) -> date:
        return date.fromisoformat(self.joined_before)


class SettingsStore:
    """Thread-safe holder for :class:`AppSettings` backed by a JSON file."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._settings = self._load()

    def _load(self) -> AppSettings:
        settings = AppSettings()
        if self._path.exists():
            try:
                data = json.loads(self._path.read_text(encoding="utf-8"))
                known = {f.name for f in fields(AppSettings)}
                for key, value in data.items():
                    if key in known:
                        setattr(settings, key, value)
            except (OSError, ValueError, TypeError) as exc:
                log.warning("Could not read settings file (%s); using defaults", exc)
        return settings.normalized()

    def get(self) -> AppSettings:
        with self._lock:
            return AppSettings(**asdict(self._settings))

    def update(self, **changes) -> AppSettings:
        with self._lock:
            current = asdict(self._settings)
            current.update(changes)
            self._settings = AppSettings(**current).normalized()
            snapshot = AppSettings(**asdict(self._settings))
        self._save(snapshot)
        return snapshot

    def _save(self, settings: AppSettings) -> None:
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(settings), indent=2), encoding="utf-8")
        tmp.replace(self._path)
