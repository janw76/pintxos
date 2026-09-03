"""Configuration: env vars > settings table > built-in defaults."""

from __future__ import annotations

import os
from pathlib import Path

# Keys are the env var names; values are the built-in defaults (None = optional).
DEFAULTS: dict[str, str | None] = {
    "PINTXOS_DATA_DIR": "./data",
    "ANTHROPIC_API_KEY": None,
    "PINTXOS_MODEL": "claude-haiku-4-5-20251001",
    "PINTXOS_POLL_MINUTES": "30",
    "PINTXOS_ITEMS_PER_FEED": "50",
    "PINTXOS_BASE_URL": None,
    "PINTXOS_FILTER_ADS": "0",
    "PINTXOS_AD_TITLE_PATTERNS": "",
    "PINTXOS_AD_KEEP_PATTERNS": "",
    # These two are environment-only (like PINTXOS_DATA_DIR): read directly by
    # pintxos/cli.py before the app/DB is touched, never via get_setting().
    "PINTXOS_HOST": "127.0.0.1",
    "PINTXOS_PORT": "8000",
}


def is_truthy(value: str | None) -> bool:
    """True if `value` looks like an enabled boolean flag ("1", "true", "yes", "on")."""
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def get_setting(key: str, conn=None) -> str | None:
    """Resolve a setting: env var wins, then the settings table, then the default."""
    env = os.environ.get(key)
    if env:
        return env
    if conn is None and key != "PINTXOS_DATA_DIR":
        from pintxos.db import db  # lazy: db imports this module

        with db() as own:
            return get_setting(key, own)
    if conn is not None:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        if row is not None and row["value"]:
            return row["value"]
    return DEFAULTS.get(key)


# ponytail: data_dir()/db_path() are functions, not module constants, so PINTXOS_DATA_DIR
# stays overridable after import (tests monkeypatch it per-case). Ceiling: callers must
# call them each time instead of importing a DATA_DIR constant.
def data_dir() -> Path:
    """Data directory, created if missing."""
    path = Path(get_setting("PINTXOS_DATA_DIR"))
    path.mkdir(parents=True, exist_ok=True)
    return path


def db_path() -> Path:
    return data_dir() / "pintxos.db"
