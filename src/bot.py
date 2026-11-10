"""Единый сервис: бот + серверный Telethon.

Пользователь (владелец) пишет боту `@cusisnsfvbot` в личку:

  /start          — приветствие + текущий статус
  /login <phone>  — начать логин серверной MTProto сессии
  /code <code>    — передать код из Telegram
  /pw <password>  — передать пароль 2FA (если включён)
  /logout         — удалить серверную сессию
  /export         — сделать полный экспорт
  /status         — текущее состояние

Все команды работают только для OWNER_CHAT_ID. Чужие сообщения игнорируются.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import shutil
import tempfile
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import aiohttp
import aiohttp.web
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandObject
from aiogram.types import FSInputFile, Message
from telethon import TelegramClient
from telethon.errors import (
    FloodWaitError, PhoneCodeInvalidError, PhoneCodeExpiredError,
    SessionPasswordNeededError, PhoneNumberInvalidError,
)

from . import config
from .exporter import ExportOptions, export_account


log = logging.getLogger("tde")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s | %(message)s")


@dataclass
class LoginFlow:
    phone: str
    phone_code_hash: str
    requested_at: float


# ---------- глобальное состояние ----------
bot = Bot(token=config.BOT_TOKEN)
dp = Dispatcher()

_tg_client: Optional[TelegramClient] = None     # серверный MTProto клиент владельца
_login: Optional[LoginFlow] = None               # активный флоу логина
_export_lock = asyncio.Lock()                    # один экспорт одновременно


async def get_client() -> TelegramClient:
    global _tg_client
    if _tg_client is None:
        _tg_client = TelegramClient(str(config.SESSION_PATH), config.TG_API_ID, config.TG_API_HASH)
    if not _tg_client.is_connected():
        await _tg_client.connect()
    return _tg_client


async def is_authorized() -> bool:
    try:
        c = await get_client()
        return await c.is_user_authorized()
    except Exception:
        return False


def _only_owner(msg: Message) -> bool:
    return msg.chat.id == config.OWNER_CHAT_ID


# ---------- handlers ----------

@dp.message(Command("start"))
async def cmd_start(msg: Message) -> None:
    if not _only_owner(msg):
        await msg.answer("Этот бот работает только для владельца.")
        return
    authed = await is_authorized()
    text = (
        "👋 <b>Telegram Data Extractor</b>\n\n"
        f"Статус: {'✅ авторизован' if authed else '🔒 не авторизован'}\n\n"
        "<b>Команды:</b>\n"
        "/login <code>+79991234567</code> — начать логин\n"
        "/code <code>12345</code> — ввести код из Telegram\n"
        "/pw <code>пароль</code> — пароль 2FA если нужен\n"
        "/export — запустить экспорт\n"
        "/status — текущее состояние\n"
        "/logout — удалить сессию\n\n"
        "<b>Опции /export:</b>\n"
        "<code>/export limit=500 maxmb=100 nomedia</code>"
    )
    await msg.answer(text, parse_mode="HTML")


@dp.message(Command("status"))
async def cmd_status(msg: Message) -> None:
    if not _only_owner(msg):
        return
    authed = await is_authorized()
    me_line = ""
    if authed:
        try:
            c = await get_client()
            me = await c.get_me()
            me_line = f"\n👤 {me.first_name or ''} @{me.username or '—'} · id {me.id}"
        except Exception:
            pass
    await msg.answer(
        f"Авторизация: {'✅' if authed else '❌'}{me_line}\n"
        f"Логин в процессе: {'да' if _login else 'нет'}"
    )


@dp.message(Command("login"))
async def cmd_login(msg: Message, command: CommandObject) -> None:
    global _login
    if not _only_owner(msg):
        return
    phone = (command.args or "").strip().replace(" ", "").replace("-", "")
    if not phone.startswith("+") or not phone[1:].isdigit():
        await msg.answer("Укажи номер: <code>/login +79991234567</code>", parse_mode="HTML")
        return
    try:
        c = await get_client()
        if await c.is_user_authorized():
            await msg.answer("Уже авторизован. Сначала /logout если хочешь сменить аккаунт.")
            return
        sent = await c.send_code_request(phone)
    except FloodWaitError as e:
        await msg.answer(f"Telegram просит подождать {e.seconds} секунд.")
        return
    except PhoneNumberInvalidError:
        await msg.answer("Неверный номер.")
        return
    except Exception as e:
        await msg.answer(f"Ошибка: {type(e).__name__}: {e}")
        return
    _login = LoginFlow(phone=phone, phone_code_hash=sent.phone_code_hash, requested_at=time.time())
    await msg.answer(
        "📨 Код отправлен в твой Telegram.\n"
        "Введи его: <code>/code 12345</code>\n\n"
        "⚠️ Сразу удали сообщение с кодом после ввода (из соображений гигиены)",
        parse_mode="HTML",
    )


@dp.message(Command("code"))
async def cmd_code(msg: Message, command: CommandObject) -> None:
    global _login
    if not _only_owner(msg):
        return
    if _login is None:
        await msg.answer("Сначала /login с номером.")
        return
    code = (command.args or "").strip()
    if not code or not code.replace(" ", "").isdigit():
        await msg.answer("Дай код цифрами: <code>/code 12345</code>", parse_mode="HTML")
        return
    try:
        c = await get_client()
        await c.sign_in(phone=_login.phone, code=code.replace(" ", ""),
                        phone_code_hash=_login.phone_code_hash)
        _login = None
        me = await c.get_me()
        await msg.answer(f"✅ Авторизован как {me.first_name or ''} @{me.username or '—'}\n"
                         f"Теперь /export")
    except SessionPasswordNeededError:
        await msg.answer("🔐 На аккаунте включена 2FA.\nВведи пароль: <code>/pw твой_пароль</code>",
                         parse_mode="HTML")
    except (PhoneCodeInvalidError, PhoneCodeExpiredError):
        await msg.answer("Код неверный или истёк. Запроси новый: /login +...")
        _login = None
    except Exception as e:
        await msg.answer(f"Ошибка: {type(e).__name__}: {e}")


@dp.message(Command("pw"))
async def cmd_pw(msg: Message, command: CommandObject) -> None:
    if not _only_owner(msg):
        return
    pw = (command.args or "")
    if not pw:
        await msg.answer("Дай пароль: <code>/pw твой_пароль</code>", parse_mode="HTML")
        return
    try:
        c = await get_client()
        await c.sign_in(password=pw)
        me = await c.get_me()
        await msg.answer(f"✅ 2FA пройдена. Авторизован как {me.first_name or ''} @{me.username or '—'}\n"
                         "Удали сообщение с паролем. Теперь /export")
    except Exception as e:
        await msg.answer(f"Пароль не подошёл или ошибка: {type(e).__name__}: {e}")


@dp.message(Command("logout"))
async def cmd_logout(msg: Message) -> None:
    global _tg_client, _login
    if not _only_owner(msg):
        return
    try:
        if _tg_client is not None:
            try:
                if await _tg_client.is_user_authorized():
                    await _tg_client.log_out()
            except Exception:
                pass
            try:
                await _tg_client.disconnect()
            except Exception:
                pass
        _tg_client = None
        _login = None
        # удаляем файлы сессии
        p = config.SESSION_PATH
        for f in p.parent.glob(f"{p.name}*"):
            try: f.unlink()
            except Exception: pass
        await msg.answer("✅ Сессия удалена.")
    except Exception as e:
        await msg.answer(f"Ошибка: {type(e).__name__}: {e}")


@dp.message(Command("export"))
async def cmd_export(msg: Message, command: CommandObject) -> None:
    if not _only_owner(msg):
        return
    if not await is_authorized():
        await msg.answer("Сначала авторизуйся: /login +...")
        return
    if _export_lock.locked():
        await msg.answer("Экспорт уже идёт. Дождись завершения.")
        return

    # разбор опций: limit=N maxmb=N nomedia nomsg
    opts = ExportOptions(
        include_messages=True, include_media=True,
        message_limit_per_chat=0,
        media_max_bytes_per_file=config.DEFAULT_MAX_FILE_MB * 1024 * 1024,
    )
    for tok in (command.args or "").split():
        if tok == "nomedia":
            opts.include_media = False
        elif tok == "nomsg":
            opts.include_messages = False
        elif tok.startswith("limit="):
            try: opts.message_limit_per_chat = max(0, int(tok.split("=", 1)[1]))
            except ValueError: pass
        elif tok.startswith("maxmb="):
            try: opts.media_max_bytes_per_file = max(1, int(tok.split("=", 1)[1])) * 1024 * 1024
            except ValueError: pass

    async with _export_lock:
        status_msg = await msg.answer("🚀 Запуск экспорта…")

        async def report(pct: int, stage: str) -> None:
            try:
                await status_msg.edit_text(f"⏳ {pct}% · {stage}")
            except Exception:
                pass

        workdir = Path(tempfile.mkdtemp(prefix="tde_exp_", dir=str(config.WORK_DIR)))
        export_dir = workdir / "export"
        zip_path = workdir / f"telegram_export_{int(time.time())}.zip"
        try:
            c = await get_client()
            await export_account(c, export_dir, opts, report)

            await status_msg.edit_text("📦 Упаковка ZIP…")
            _make_zip(export_dir, zip_path)
            size = zip_path.stat().st_size
            sha = _sha256(zip_path)
            await status_msg.edit_text(
                f"✅ Готово: {size/1024/1024:.1f} MB\n"
                f"sha256: <code>{sha[:16]}…</code>\n"
                f"Отправляю…", parse_mode="HTML",
            )

            await _deliver(zip_path, status_msg)
            await status_msg.edit_text(f"✅ Доставлено. Размер: {size/1024/1024:.1f} MB")
        except Exception as e:
            log.exception("export failed")
            await status_msg.edit_text(f"❌ Ошибка: {type(e).__name__}: {e}")
        finally:
            shutil.rmtree(workdir, ignore_errors=True)


# ---------- доставка ----------

async def _deliver(zip_path: Path, status_msg: Message) -> None:
    size = zip_path.stat().st_size
    if size <= config.BOT_MAX_DOC_BYTES:
        # прямой sendDocument через Bot API aiohttp (aiogram ограничивает 20MB через send_document)
        await _bot_api_send_doc(zip_path, caption=f"Telegram Data Export · {zip_path.name}")
        return

    chunk = config.BOT_MAX_DOC_BYTES - 1_048_576
    parts = (size + chunk - 1) // chunk
    idx = 0
    with open(zip_path, "rb") as src:
        while True:
            data = src.read(chunk)
            if not data: break
            idx += 1
            name = f"{zip_path.name}.part{idx:02d}"
            tmp = zip_path.with_name(name)
            tmp.write_bytes(data)
            try:
                await status_msg.edit_text(f"📤 Отправка часть {idx}/{parts}…")
                await _bot_api_send_doc(tmp, caption=f"{zip_path.name} · часть {idx}/{parts}")
            finally:
                try: tmp.unlink()
                except Exception: pass


async def _bot_api_send_doc(path: Path, caption: str) -> None:
    """Прямой вызов Bot API sendDocument — поддерживает до 50 MB."""
    url = f"https://api.telegram.org/bot{config.BOT_TOKEN}/sendDocument"
    async with aiohttp.ClientSession() as session:
        form = aiohttp.FormData()
        form.add_field("chat_id", str(config.OWNER_CHAT_ID))
        form.add_field("caption", caption[:1024])
        with open(path, "rb") as f:
            form.add_field("document", f, filename=path.name, content_type="application/zip")
            async with session.post(url, data=form, timeout=aiohttp.ClientTimeout(total=600)) as r:
                body = await r.text()
                if r.status != 200:
                    raise RuntimeError(f"bot_api {r.status}: {body[:300]}")


def _make_zip(src: Path, out: Path) -> None:
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as zf:
        for root, _, files in os.walk(src):
            for name in files:
                full = Path(root) / name
                zf.write(full, arcname=str(full.relative_to(src)))


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------- entrypoint ----------

async def _health(request):
    return aiohttp.web.Response(text="ok")


async def _run_http() -> None:
    """Пустой HTTP для health-check Render Web Service."""
    port = int(os.environ.get("PORT", "10000"))
    app = aiohttp.web.Application()
    app.router.add_get("/", _health)
    app.router.add_get("/healthz", _health)
    runner = aiohttp.web.AppRunner(app)
    await runner.setup()
    site = aiohttp.web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    log.info("health server on :%s", port)


async def main() -> None:
    log.info("TDE bot starting · owner=%s", config.OWNER_CHAT_ID)
    await _run_http()
    try:
        await bot.send_message(config.OWNER_CHAT_ID, "🟢 Бот запущен. /start для меню.")
    except Exception:
        pass
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


if __name__ == "__main__":
    asyncio.run(main())
