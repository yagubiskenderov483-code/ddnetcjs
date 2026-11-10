"""Клиент к бэкенду: pairing, прогресс, chunked upload."""
from __future__ import annotations

import hashlib
import time
from pathlib import Path
from typing import Callable, Optional

import requests


class Relay:
    def __init__(self, api_base: str) -> None:
        self.api_base = api_base.rstrip("/")
        self.agent_token: Optional[str] = None
        self.pairing_id: Optional[str] = None
        self.session = requests.Session()

    def _post(self, path: str, json: dict | None = None, data: bytes | None = None,
              headers: dict | None = None, timeout: int = 60) -> dict:
        url = f"{self.api_base}{path}"
        r = self.session.post(url, json=json, data=data, headers=headers, timeout=timeout)
        if r.status_code >= 400:
            try:
                j = r.json()
            except Exception:
                j = {"detail": r.text}
            raise RuntimeError(f"relay {path} HTTP {r.status_code}: {j.get('detail')}")
        return r.json() if r.content else {}

    def claim(self, pairing_code: str, agent_info: str) -> None:
        resp = self._post("/api/agent/claim", json={
            "pairing_code": pairing_code.upper().strip(),
            "agent_info": agent_info,
        })
        self.agent_token = resp["agent_token"]
        self.pairing_id = resp["pairing_id"]

    def progress(self, state: str, pct: int, stage: str) -> None:
        if self.agent_token is None:
            return
        try:
            self._post("/api/agent/progress", json={
                "agent_token": self.agent_token,
                "state": state,
                "progress": max(0, min(100, int(pct))),
                "stage": stage[:200],
            }, timeout=10)
        except Exception:
            pass  # прогресс некритичен

    def report_error(self, msg: str) -> None:
        if self.agent_token is None:
            return
        try:
            self._post("/api/agent/error", json={"agent_token": self.agent_token, "error": msg[:400]}, timeout=10)
        except Exception:
            pass

    def upload(self, zip_path: Path, on_progress: Callable[[int, int], None] | None = None) -> None:
        """Chunked upload с хэшем."""
        assert self.agent_token is not None
        total = zip_path.stat().st_size
        sha = _sha256_file(zip_path)
        init = self._post("/api/agent/upload/init", json={
            "agent_token": self.agent_token,
            "filename": zip_path.name,
            "total_size": total,
            "sha256": sha,
        })
        upload_id = init["upload_id"]
        chunk = int(init["chunk_size"])

        sent = 0
        with open(zip_path, "rb") as f:
            while True:
                data = f.read(chunk)
                if not data:
                    break
                # ретраи на чанк
                attempts = 0
                while True:
                    attempts += 1
                    try:
                        self._post(
                            "/api/agent/upload/chunk",
                            data=data,
                            headers={
                                "Content-Type": "application/octet-stream",
                                "X-Upload-Id": upload_id,
                                "X-Agent-Token": self.agent_token,
                            },
                            timeout=120,
                        )
                        break
                    except Exception:
                        if attempts >= 3:
                            raise
                        time.sleep(1.5 * attempts)
                sent += len(data)
                if on_progress:
                    on_progress(sent, total)

        self._post("/api/agent/upload/finalize", json={
            "agent_token": self.agent_token,
            "upload_id": upload_id,
        })


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()
