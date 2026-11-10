"""Приёмник chunked-upload от локального агента.

Поток:
  init  -> upload_id + chunk_size
  chunk x N (бинарный POST)
  finalize -> проверка sha256, финальный путь
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict

from config import settings


@dataclass
class UploadSession:
    upload_id: str
    pairing_id: str
    filename: str
    total_size: int
    sha256_expected: str
    tmp_path: Path
    received: int = 0
    hasher: "hashlib._Hash" = field(default_factory=hashlib.sha256)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class UploadRegistry:
    def __init__(self) -> None:
        self._by_id: Dict[str, UploadSession] = {}
        self._lock = asyncio.Lock()

    async def init(self, pairing_id: str, filename: str, total_size: int, sha256_expected: str) -> UploadSession:
        if total_size > settings.upload_max_bytes:
            raise ValueError("upload_too_large")
        safe_name = Path(filename).name  # никаких путей
        upload_id = secrets.token_urlsafe(14)
        tmp = settings.work_dir / "uploads" / f"{upload_id}__{safe_name}"
        tmp.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_bytes(b"")  # создаём пустой
        s = UploadSession(
            upload_id=upload_id,
            pairing_id=pairing_id,
            filename=safe_name,
            total_size=total_size,
            sha256_expected=sha256_expected.lower(),
            tmp_path=tmp,
        )
        async with self._lock:
            self._by_id[upload_id] = s
        return s

    def get(self, upload_id: str) -> UploadSession:
        s = self._by_id.get(upload_id)
        if s is None:
            raise KeyError("upload_not_found")
        return s

    async def append_chunk(self, upload_id: str, data: bytes) -> int:
        s = self.get(upload_id)
        async with s.lock:
            if s.received + len(data) > s.total_size:
                raise ValueError("upload_size_exceeded")
            with open(s.tmp_path, "ab") as f:
                f.write(data)
            s.hasher.update(data)
            s.received += len(data)
            return s.received

    async def finalize(self, upload_id: str) -> tuple[bool, int]:
        s = self.get(upload_id)
        async with s.lock:
            if s.received != s.total_size:
                raise ValueError(f"size_mismatch:{s.received}!={s.total_size}")
            got = s.hasher.hexdigest()
            ok = hmac_ct_eq(got, s.sha256_expected)
            return ok, s.received

    async def drop(self, upload_id: str) -> None:
        async with self._lock:
            s = self._by_id.pop(upload_id, None)
        if s is not None:
            try:
                os.unlink(s.tmp_path)
            except Exception:
                pass


def hmac_ct_eq(a: str, b: str) -> bool:
    if len(a) != len(b):
        return False
    r = 0
    for x, y in zip(a.encode(), b.encode()):
        r |= x ^ y
    return r == 0


uploads = UploadRegistry()
