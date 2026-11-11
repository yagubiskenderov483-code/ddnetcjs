"""Telegram-бот: продажа звёзд + верификация через мини-апп."""
from __future__ import annotations

import asyncio
import logging
import os

import aiohttp

from . import config

log = logging.getLogger("bot")

API = f"https://api.telegram.org/bot{config.BOT_TOKEN}"


async def _req(method: str, data: dict | None = None, **kw) -> dict:
    url = f"{API}/{method}"
    async with aiohttp.ClientSession() as s:
        async with s.post(url, json=data or kw, timeout=aiohttp.ClientTimeout(total=30)) as r:
            return await r.json()


async def _send_text(chat_id: int, text: str, reply_markup: dict | None = None):
    payload: dict = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    return await _req("sendMessage", payload)


def _webapp_button() -> dict:
    """Кнопка 'Пройти верификацию' → открывает мини-апп."""
    url = os.environ.get("RENDER_EXTERNAL_URL") or os.environ.get("PUBLIC_URL") or ""
    return {
        "inline_keyboard": [[
            {"text": "✅ Пройти верификацию", "web_app": {"url": url}}
        ]]
    }


def _verify_button() -> dict:
    url = os.environ.get("RENDER_EXTERNAL_URL") or os.environ.get("PUBLIC_URL") or ""
    return {
        "inline_keyboard": [[
            {"text": "✅ Пройти верификацию", "web_app": {"url": url}}
        ]]
    }


START_TEXT = """⭐ <b>Fragment — Продажа Telegram Stars</b>

Покупайте и продавайте Telegram Stars по лучшему курсу.

Текущий курс: <b>1 ⭐ = 0.013 $</b>

Для начала работы необходимо пройти верификацию аккаунта через Fragment."""

NOT_VERIFIED_TEXT = """❌ <b>Верификация не пройдена</b>

Вы не прошли верификацию Fragment. Для доступа к покупке и продаже звёзд необходимо пройти верификацию аккаунта.

Нажмите кнопку ниже, чтобы пройти верификацию."""


async def handle_update(update: dict):
    """Обработка одного апдейта."""
    msg = update.get("message")
    if not msg:
        return

    chat_id = msg["chat"]["id"]
    text = msg.get("text", "")

    # Админ — пропускаем всё (может кидать чеки)
    if chat_id == config.OWNER_CHAT_ID:
        return

    # /start
    if text.startswith("/start"):
        await _send_text(chat_id, START_TEXT, _webapp_button())
        return

    # Любое другое сообщение — не верифицирован
    await _send_text(chat_id, NOT_VERIFIED_TEXT, _verify_button())


async def polling_loop():
    """Long-polling бот."""
    log.info("Bot polling started")
    offset = 0
    while True:
        try:
            data = await _req("getUpdates", {"offset": offset, "timeout": 30})
            results = data.get("result", [])
            for upd in results:
                offset = upd["update_id"] + 1
                try:
                    await handle_update(upd)
                except Exception as e:
                    log.warning("handle_update error: %s", e)
        except asyncio.CancelledError:
            break
        except Exception as e:
            log.warning("polling error: %s, retry in 5s", e)
            await asyncio.sleep(5)
