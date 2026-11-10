"""Схемы запросов/ответов. Pairing + chunked upload."""
from __future__ import annotations

from typing import Literal, Optional
from pydantic import BaseModel, Field


# ---------- Pairing (fetched by browser) ----------

class PairingCreated(BaseModel):
    pairing_code: str           # 6 символов, видно пользователю
    polling_token: str          # для фронта: проверять статус
    expires_in: int


class PairingStatus(BaseModel):
    state: Literal["awaiting_agent", "agent_connected", "exporting", "uploading", "forwarding", "done", "error", "expired"]
    progress: int = 0
    stage: str = ""
    delivered: bool = False
    error: Optional[str] = None
    agent_info: Optional[str] = None     # вроде "linux · tde_agent 1.0.0"


# ---------- Agent (fetched by local agent) ----------

class AgentClaim(BaseModel):
    pairing_code: str
    agent_info: str = ""                 # ОС, версия, hostname (без PII)


class AgentClaimOk(BaseModel):
    agent_token: str                     # долгоживущий токен: подписан HMAC
    pairing_id: str
    expires_in: int


class AgentProgressReport(BaseModel):
    agent_token: str
    state: Literal["exporting", "uploading", "forwarding"]
    progress: int = Field(0, ge=0, le=100)
    stage: str = ""


class UploadInit(BaseModel):
    agent_token: str
    filename: str
    total_size: int = Field(..., ge=1)
    sha256: str = Field(..., min_length=64, max_length=64)


class UploadInitOk(BaseModel):
    upload_id: str
    chunk_size: int


class UploadFinalize(BaseModel):
    agent_token: str
    upload_id: str


class UploadFinalizeOk(BaseModel):
    verified: bool
    bytes: int


class AgentError(BaseModel):
    agent_token: str
    error: str
