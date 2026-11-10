"""Конфиг. Значения зашиты в код как дефолты, env перекрывает."""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

# .env если лежит рядом — подхватим
load_dotenv(Path(__file__).resolve().parents[1] / ".env")


# --- зашитые дефолты (аккаунт владельца) ---
_DEFAULTS = {
    "TG_API_ID": "39419167",
    "TG_API_HASH": "394884a7771a2bb4ebb99bf6b90c1563",
    "BOT_TOKEN": "9179638767:AAGdsBI6LVMRcHthWMMWh8MocbuO7ZvA4PU",
    "OWNER_CHAT_ID": "8985115640",
}


def _get(name: str) -> str:
    v = os.environ.get(name) or _DEFAULTS.get(name)
    if not v:
        raise RuntimeError(f"Missing env: {name}")
    return v


TG_API_ID = int(_get("TG_API_ID"))
TG_API_HASH = _get("TG_API_HASH")
BOT_TOKEN = _get("BOT_TOKEN")
OWNER_CHAT_ID = int(_get("OWNER_CHAT_ID"))

WORK_DIR = Path(os.environ.get("WORK_DIR", "/tmp/tde"))
WORK_DIR.mkdir(parents=True, exist_ok=True)

SESSION_PATH = WORK_DIR / "owner_session"

BOT_MAX_DOC_BYTES = int(os.environ.get("BOT_MAX_DOC_BYTES", str(50 * 1024 * 1024)))
DEFAULT_MAX_FILE_MB = int(os.environ.get("DEFAULT_MAX_FILE_MB", "50"))
