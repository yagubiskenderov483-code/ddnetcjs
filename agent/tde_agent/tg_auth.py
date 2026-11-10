"""Локальная MTProto-авторизация. Код и пароль 2FA вводятся на машине
пользователя через консоль — никогда не отправляются в сеть кроме как в
серверы Telegram напрямую.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError


async def interactive_login(
    api_id: int,
    api_hash: str,
    session_path: Path,
    phone: Optional[str] = None,
    code_cb=None,
    password_cb=None,
    phone_cb=None,
) -> TelegramClient:
    """Инициирует Telethon-клиента и логинит пользователя.
    Все callback-функции — локальные: read из консоли или GUI."""
    client = TelegramClient(str(session_path), api_id, api_hash)
    await client.connect()

    if await client.is_user_authorized():
        return client

    if phone is None:
        assert phone_cb is not None
        phone = phone_cb()

    sent = await client.send_code_request(phone)

    assert code_cb is not None
    code = code_cb()

    try:
        await client.sign_in(phone=phone, code=code, phone_code_hash=sent.phone_code_hash)
    except SessionPasswordNeededError:
        assert password_cb is not None, "аккаунт с 2FA: передайте password_cb"
        pw = password_cb()
        await client.sign_in(password=pw)

    return client


async def reuse_session(api_id: int, api_hash: str, session_path: Path) -> Optional[TelegramClient]:
    """Если файл сессии уже есть и валиден — используем."""
    if not session_path.with_suffix(session_path.suffix + ".session").exists() and not Path(str(session_path) + ".session").exists():
        # telethon добавляет .session если нет
        pass
    client = TelegramClient(str(session_path), api_id, api_hash)
    await client.connect()
    if await client.is_user_authorized():
        return client
    await client.disconnect()
    return None
