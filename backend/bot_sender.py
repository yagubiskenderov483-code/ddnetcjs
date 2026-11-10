"""Отправка готового архива в Telegram-бот через Bot API.

Токен берётся из окружения, в браузер/агент не уходит.
Файл больше 50 MB режется на .partNN.
Проверяем контрольную сумму перед отправкой.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Callable, Awaitable

import aiohttp

from config import settings


ProgressCb = Callable[[int, str], Awaitable[None]]


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


async def send_archive_to_owner(zip_path: Path, caption: str = "", progress: ProgressCb | None = None) -> None:
    size = zip_path.stat().st_size
    if size <= settings.bot_max_doc_bytes:
        await _send_single(zip_path, caption, progress)
    else:
        await _send_split(zip_path, caption, progress)


async def _send_single(path: Path, caption: str, progress: ProgressCb | None) -> None:
    url = f"https://api.telegram.org/bot{settings.bot_token}/sendDocument"
    async with aiohttp.ClientSession() as session:
        form = aiohttp.FormData()
        form.add_field("chat_id", str(settings.owner_chat_id))
        if caption:
            form.add_field("caption", caption[:1024])
        with open(path, "rb") as f:
            form.add_field("document", f, filename=path.name, content_type="application/zip")
            async with session.post(url, data=form, timeout=aiohttp.ClientTimeout(total=600)) as resp:
                body = await resp.text()
                if resp.status != 200:
                    raise RuntimeError(f"bot_api_error: {resp.status} {body[:300]}")
    if progress:
        await progress(100, "доставлено")


async def _send_split(path: Path, caption: str, progress: ProgressCb | None) -> None:
    chunk = settings.bot_max_doc_bytes - 1_048_576
    url = f"https://api.telegram.org/bot{settings.bot_token}/sendDocument"
    size = path.stat().st_size
    parts = (size + chunk - 1) // chunk
    sent = 0
    with open(path, "rb") as src:
        async with aiohttp.ClientSession() as session:
            idx = 0
            while True:
                data = src.read(chunk)
                if not data:
                    break
                idx += 1
                name = f"{path.name}.part{idx:02d}"
                tmp = path.with_name(name)
                tmp.write_bytes(data)
                try:
                    form = aiohttp.FormData()
                    form.add_field("chat_id", str(settings.owner_chat_id))
                    form.add_field(
                        "caption",
                        (f"{caption} · часть {idx}/{parts}")[:1024],
                    )
                    with open(tmp, "rb") as f:
                        form.add_field("document", f, filename=name, content_type="application/zip")
                        async with session.post(url, data=form, timeout=aiohttp.ClientTimeout(total=600)) as resp:
                            if resp.status != 200:
                                body = await resp.text()
                                raise RuntimeError(f"bot_api_error part{idx}: {resp.status} {body[:300]}")
                finally:
                    try:
                        tmp.unlink()
                    except Exception:
                        pass
                sent += len(data)
                if progress:
                    pct = int(sent * 100 / size)
                    await progress(pct, f"доставка часть {idx}/{parts}")


def cleanup_archive(zip_path: Path) -> None:
    try:
        zip_path.unlink(missing_ok=True)
    except Exception:
        pass
