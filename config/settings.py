"""
config/settings.py - one place for paths, .env loading and typed env helpers.

Importing this module loads <project>/.env (values already set in the real environment win).
Every variable name from the previous Alpha versions still works (ALPHA_MIC, GEMINI_API_KEY,
ANTHROPIC_API_KEY, SERVER_TOKEN / ALPHA_WEB_TOKEN, N8N_*, ALPHA_* tuning knobs).
"""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load_env_file(path: Path) -> None:
    """KEY=VALUE per line; '#' comments and trailing ' # comments' are ignored."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.split(" #")[0].strip().strip('"').strip("'"))


load_env_file(ROOT / ".env")

DATA_DIR = Path(os.environ.get("ALPHA_DATA_DIR") or ROOT / "data")
DATA_DIR.mkdir(parents=True, exist_ok=True)

DB_PATH = Path(os.environ.get("ALPHA_DB") or DATA_DIR / "alpha_memory.db")
VOICE_PROFILE = Path(os.environ.get("ALPHA_VOICE_PROFILE") or DATA_DIR / "voice_profile.npz")
WHATSAPP_PROFILE = Path(os.environ.get("WHATSAPP_PROFILE_DIR") or DATA_DIR / "whatsapp_profile")
CONTACTS_FILE = Path(os.environ.get("ALPHA_CONTACTS") or ROOT / "config" / "contacts.json")
UI_DIR = ROOT / "ui"
LOG_FILE = DATA_DIR / "alpha.log"


def env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


def env_int(name: str, default: int) -> int:
    return int(env_float(name, default))


def env_bool(name: str, default: bool = False) -> bool:
    v = os.environ.get(name)
    if v is None or v.strip() == "":
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def fixed_token() -> str:
    """SERVER_TOKEN pins the dashboard/API token (ALPHA_WEB_TOKEN is the old alias)."""
    return (os.environ.get("SERVER_TOKEN") or os.environ.get("ALPHA_WEB_TOKEN") or "").strip()
