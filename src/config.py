"""Конфиг из окружения."""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

# .env рядом с репо
load_dotenv(Path(__file__).resolve().parents[1] / ".env")


def _req(name: str) -> str:
    v = os.environ.get(name)
    if not v:
        raise RuntimeError(f"Missing env: {name}")
    return v


TG_API_ID = int(_req("TG_API_ID"))
TG_API_HASH = _req("TG_API_HASH")
BOT_TOKEN = _req("BOT_TOKEN")
OWNER_CHAT_ID = int(_req("OWNER_CHAT_ID"))

WORK_DIR = Path(os.environ.get("WORK_DIR", "/tmp/tde"))
WORK_DIR.mkdir(parents=True, exist_ok=True)

SESSION_PATH = WORK_DIR / "owner_session"

# Лимит Bot API sendDocument
BOT_MAX_DOC_BYTES = int(os.environ.get("BOT_MAX_DOC_BYTES", str(50 * 1024 * 1024)))

# Пропускать медиа крупнее (можно переопределить в /export)
DEFAULT_MAX_FILE_MB = int(os.environ.get("DEFAULT_MAX_FILE_MB", "50"))
