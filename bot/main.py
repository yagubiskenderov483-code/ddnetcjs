"""Приёмный бот. Не обязателен для работы сервиса — архивы бэкенд шлёт
через Bot API напрямую, но бот отвечает на /start, показывает статус
получения и подтверждает доставку владельцу.

Запускать на том же токене, что указан в BOT_TOKEN. OWNER_CHAT_ID — тот,
кому можно слать команды.
"""
from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Document, Message
from dotenv import load_dotenv


load_dotenv(dotenv_path=Path(__file__).resolve().parents[1] / ".env")

BOT_TOKEN = os.environ["BOT_TOKEN"]
OWNER_CHAT_ID = int(os.environ["OWNER_CHAT_ID"])

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s | %(message)s")
log = logging.getLogger("tde-bot")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()


@dp.message(Command("start"))
async def cmd_start(message: Message) -> None:
    if message.chat.id != OWNER_CHAT_ID:
        await message.answer("Этот бот обслуживает только владельца сервиса.")
        return
    await message.answer(
        "Telegram Data Extractor bot активен.\n"
        "Сюда будут приходить ZIP-архивы экспорта, запрошенные пользователями на сайте.\n\n"
        f"Ваш chat_id: <code>{message.chat.id}</code>",
        parse_mode="HTML",
    )


@dp.message(Command("id"))
async def cmd_id(message: Message) -> None:
    await message.answer(f"chat_id: <code>{message.chat.id}</code>", parse_mode="HTML")


@dp.message(F.document)
async def on_document(message: Message) -> None:
    doc: Document = message.document  # type: ignore[assignment]
    if message.chat.id != OWNER_CHAT_ID:
        return
    log.info("archive received: %s (%d bytes)", doc.file_name, doc.file_size or 0)
    await message.reply(f"Архив <b>{doc.file_name}</b> получен ({(doc.file_size or 0)/1024:.1f} KB)", parse_mode="HTML")


async def main() -> None:
    log.info("bot started (owner=%s)", OWNER_CHAT_ID)
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


if __name__ == "__main__":
    asyncio.run(main())
