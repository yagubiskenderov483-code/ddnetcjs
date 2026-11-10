"""Конфигурация бэкенда."""
from __future__ import annotations

from pathlib import Path
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Bot API — токен ТОЛЬКО на сервере
    bot_token: str = Field(..., alias="BOT_TOKEN")
    owner_chat_id: int = Field(..., alias="OWNER_CHAT_ID")

    backend_host: str = Field("0.0.0.0", alias="BACKEND_HOST")
    backend_port: int = Field(8000, alias="BACKEND_PORT")

    frontend_origin: str = Field("http://localhost:3000", alias="FRONTEND_ORIGIN")
    work_dir: Path = Field(Path("/tmp/tde"), alias="WORK_DIR")

    # Pairing + upload
    pairing_ttl_seconds: int = Field(900, alias="PAIRING_TTL_SECONDS")       # 15 мин жизнь кода
    agent_token_ttl_seconds: int = Field(14_400, alias="AGENT_TOKEN_TTL_SECONDS")  # 4 часа агенту
    upload_max_bytes: int = Field(4 * 1024 * 1024 * 1024, alias="UPLOAD_MAX_BYTES")  # 4 GB
    chunk_max_bytes: int = Field(8 * 1024 * 1024, alias="CHUNK_MAX_BYTES")   # 8 MB per chunk
    temp_ttl_seconds: int = Field(600, alias="TEMP_TTL_SECONDS")
    # 50 MB — лимит Bot API sendDocument
    bot_max_doc_bytes: int = Field(50 * 1024 * 1024, alias="BOT_MAX_DOC_BYTES")

    session_secret: str = Field(..., alias="SESSION_SECRET")

    # URL, по которому агент стучится на бэк (прописывается в агенте)
    public_api_base: str = Field("http://localhost:8000", alias="PUBLIC_API_BASE")


settings = Settings()
settings.work_dir.mkdir(parents=True, exist_ok=True)
(settings.work_dir / "uploads").mkdir(parents=True, exist_ok=True)
(settings.work_dir / "archives").mkdir(parents=True, exist_ok=True)
