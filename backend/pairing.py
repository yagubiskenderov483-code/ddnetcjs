"""Pairing: сайт выдаёт код, локальный агент его забирает.

Код — 6 заглавных букв/цифр. Жизнь кода ограничена. После claim'а
агент получает долгоживущий HMAC-подписанный токен, которым авторизует
последующие вызовы (upload, прогресс).
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import secrets
import string
import time
from dataclasses import dataclass, field
from typing import Dict, Optional

from config import settings


ALPHABET = string.ascii_uppercase.replace("O", "").replace("I", "") + string.digits.replace("0", "").replace("1", "")


def _gen_code() -> str:
    return "".join(secrets.choice(ALPHABET) for _ in range(6))


@dataclass
class Pairing:
    pairing_id: str
    pairing_code: str
    polling_token: str              # для фронта
    agent_token: Optional[str] = None
    created_at: float = field(default_factory=time.time)
    claimed_at: Optional[float] = None
    state: str = "awaiting_agent"   # awaiting_agent -> agent_connected -> exporting -> uploading -> forwarding -> done | error | expired
    progress: int = 0
    stage: str = ""
    delivered: bool = False
    error: Optional[str] = None
    agent_info: Optional[str] = None
    # upload lifecycle
    upload_id: Optional[str] = None
    upload_filename: Optional[str] = None
    upload_total_size: int = 0
    upload_sha256: Optional[str] = None
    upload_path: Optional[str] = None  # итоговый zip
    archive_bytes: int = 0


class PairingStore:
    def __init__(self) -> None:
        self._by_id: Dict[str, Pairing] = {}
        self._by_code: Dict[str, str] = {}      # code -> pairing_id
        self._by_poll: Dict[str, str] = {}      # polling_token -> pairing_id
        self._lock = asyncio.Lock()

    # ---- helpers ----

    def _sign(self, raw: str) -> str:
        mac = hmac.new(settings.session_secret.encode(), raw.encode(), hashlib.sha256).hexdigest()[:24]
        return f"{raw}.{mac}"

    def _verify_token(self, token: str) -> Optional[str]:
        try:
            raw, mac = token.rsplit(".", 1)
        except ValueError:
            return None
        want = hmac.new(settings.session_secret.encode(), raw.encode(), hashlib.sha256).hexdigest()[:24]
        return raw if hmac.compare_digest(mac, want) else None

    # ---- lifecycle ----

    async def create(self) -> Pairing:
        async with self._lock:
            # до 5 попыток уникальный код
            for _ in range(5):
                code = _gen_code()
                if code not in self._by_code:
                    break
            else:
                raise RuntimeError("cannot_allocate_code")
            pid = secrets.token_urlsafe(12)
            poll = self._sign(f"poll_{pid}_{secrets.token_urlsafe(8)}")
            p = Pairing(pairing_id=pid, pairing_code=code, polling_token=poll)
            self._by_id[pid] = p
            self._by_code[code] = pid
            self._by_poll[poll] = pid
            return p

    async def claim(self, code: str, agent_info: str) -> Pairing:
        """Агент предъявляет pairing_code -> получает agent_token."""
        async with self._lock:
            pid = self._by_code.get(code.upper().strip())
            if pid is None:
                raise KeyError("pairing_not_found")
            p = self._by_id[pid]
            if p.state != "awaiting_agent":
                raise RuntimeError(f"already_claimed_or_finished:{p.state}")
            if time.time() - p.created_at > settings.pairing_ttl_seconds:
                p.state = "expired"
                raise RuntimeError("pairing_expired")

            raw = f"agent_{pid}_{secrets.token_urlsafe(16)}"
            p.agent_token = self._sign(raw)
            p.claimed_at = time.time()
            p.state = "agent_connected"
            p.agent_info = (agent_info or "")[:120]
            # код одноразовый — убираем из индекса
            self._by_code.pop(code.upper().strip(), None)
            return p

    def _pid_from_agent_token(self, token: str) -> Optional[str]:
        raw = self._verify_token(token)
        if raw is None or not raw.startswith("agent_"):
            return None
        pid = raw.split("_")[1]
        return pid if pid in self._by_id else None

    async def get_by_agent(self, token: str) -> Pairing:
        pid = self._pid_from_agent_token(token)
        if pid is None:
            raise PermissionError("invalid_agent_token")
        p = self._by_id[pid]
        if p.claimed_at and time.time() - p.claimed_at > settings.agent_token_ttl_seconds:
            raise PermissionError("agent_token_expired")
        return p

    def get_by_poll(self, polling_token: str) -> Pairing:
        pid = self._by_poll.get(polling_token)
        if pid is None:
            raise PermissionError("invalid_polling_token")
        return self._by_id[pid]

    async def update_progress(self, token: str, state: str, progress: int, stage: str) -> None:
        async with self._lock:
            p = await self.get_by_agent(token)
            p.state = state
            p.progress = max(0, min(100, int(progress)))
            p.stage = stage[:200]

    async def mark_error(self, token: str, error: str) -> None:
        async with self._lock:
            p = await self.get_by_agent(token)
            p.state = "error"
            p.error = error[:400]

    async def mark_delivered(self, token: str) -> None:
        async with self._lock:
            p = await self.get_by_agent(token)
            p.state = "done"
            p.progress = 100
            p.delivered = True

    async def prune(self) -> None:
        now = time.time()
        async with self._lock:
            for pid, p in list(self._by_id.items()):
                # необъявленный pairing истёк
                if p.state == "awaiting_agent" and now - p.created_at > settings.pairing_ttl_seconds:
                    p.state = "expired"
                    self._by_code.pop(p.pairing_code, None)
                # завершённые старше 1 часа удаляем совсем
                if p.state in ("done", "error", "expired") and now - p.created_at > 3600:
                    self._by_id.pop(pid, None)
                    self._by_poll.pop(p.polling_token, None)


store = PairingStore()
