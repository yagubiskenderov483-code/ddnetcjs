"""Полный экспорт аккаунта в формате, совместимом с Telegram Desktop export.

Структура результата:
    out/
      profile.json
      contacts.json
      chats/
        <chat_id>_<slug>/
          messages.json           # полный JSON всех сообщений
          messages.html           # человеко-читаемый HTML
          photos/                 # фотографии
          video_files/            # видео
          voice_messages/         # голосовые
          round_video_messages/   # видеосообщения (круглые)
          files/                  # документы
          stickers/               # стикеры
          animations/             # GIF/анимации
      index.html                  # главная страница с ссылками

Важно: экспорт запускается ТОЛЬКО для аккаунта, под которым залогинен
локальный клиент. Чужих данных читать невозможно.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional

from telethon import TelegramClient
from telethon.tl.types import (
    Channel,
    Chat,
    DocumentAttributeAudio,
    DocumentAttributeFilename,
    DocumentAttributeSticker,
    DocumentAttributeVideo,
    Message,
    MessageMediaDocument,
    MessageMediaPhoto,
    User,
)

from .html_renderer import render_index, render_chat


ProgressCb = Callable[[int, str], Awaitable[None]]


@dataclass
class ExportOptions:
    include_messages: bool = True
    include_media: bool = True
    message_limit_per_chat: int = 0       # 0 = без лимита
    media_max_bytes_per_file: int = 50 * 1024 * 1024  # фильтр гигантских файлов
    date_from: Optional[datetime] = None
    date_to: Optional[datetime] = None


def _slug(text: str, maxlen: int = 40) -> str:
    text = text or "untitled"
    text = re.sub(r"[^\w\-]+", "_", text, flags=re.UNICODE).strip("_")
    return text[:maxlen] or "untitled"


def _serialize_entity(entity: Any) -> Dict[str, Any]:
    if isinstance(entity, User):
        return {
            "type": "user",
            "id": entity.id,
            "first_name": entity.first_name,
            "last_name": entity.last_name,
            "username": entity.username,
            "phone": entity.phone,
            "is_bot": bool(entity.bot),
            "is_premium": bool(getattr(entity, "premium", False)),
        }
    if isinstance(entity, Channel):
        return {
            "type": "channel" if entity.broadcast else "supergroup",
            "id": entity.id,
            "title": entity.title,
            "username": entity.username,
            "megagroup": bool(entity.megagroup),
            "broadcast": bool(entity.broadcast),
        }
    if isinstance(entity, Chat):
        return {"type": "group", "id": entity.id, "title": entity.title, "participants_count": entity.participants_count}
    return {"type": "unknown", "repr": str(entity)[:200]}


def _classify_media(m: Message) -> tuple[str, str]:
    """Возвращает (subfolder, suggested_ext)."""
    media = m.media
    if isinstance(media, MessageMediaPhoto):
        return "photos", "jpg"
    if isinstance(media, MessageMediaDocument) and media.document is not None:
        doc = media.document
        mime = (doc.mime_type or "").lower()
        is_voice = False
        is_round = False
        is_sticker = False
        is_anim = False
        is_video = False
        is_audio = False
        for attr in doc.attributes:
            if isinstance(attr, DocumentAttributeAudio):
                is_voice = attr.voice
                is_audio = not attr.voice
            elif isinstance(attr, DocumentAttributeVideo):
                is_video = True
                is_round = attr.round_message
            elif isinstance(attr, DocumentAttributeSticker):
                is_sticker = True
        if "gif" in mime or any(getattr(a, "animated", False) for a in doc.attributes):
            is_anim = True

        if is_voice:
            return "voice_messages", "ogg"
        if is_round:
            return "round_video_messages", "mp4"
        if is_sticker:
            return "stickers", "webp" if "webp" in mime else "tgs"
        if is_anim:
            return "animations", "mp4"
        if is_video:
            return "video_files", "mp4"
        if is_audio:
            return "files", "mp3"
        return "files", (mime.split("/", 1)[-1][:6] or "bin")
    return "files", "bin"


def _doc_filename(m: Message, default_ext: str) -> str:
    media = m.media
    if isinstance(media, MessageMediaDocument) and media.document is not None:
        for attr in media.document.attributes:
            if isinstance(attr, DocumentAttributeFilename) and attr.file_name:
                return attr.file_name
    return f"msg_{m.id}.{default_ext}"


def _msg_to_dict(m: Message, saved_media_path: Optional[str]) -> Dict[str, Any]:
    out: Dict[str, Any] = {
        "id": m.id,
        "date": m.date.isoformat() if m.date else None,
        "edit_date": m.edit_date.isoformat() if m.edit_date else None,
        "from_id": _ser_peer(m.from_id) if m.from_id else None,
        "sender_id": m.sender_id,
        "text": m.text or "",
        "reply_to_msg_id": m.reply_to_msg_id,
    }
    if m.media is not None:
        out["media"] = {
            "type": type(m.media).__name__,
            "saved_path": saved_media_path,
        }
    if m.action is not None:
        out["action"] = type(m.action).__name__
    if m.forward is not None:
        out["forwarded_from"] = {
            "date": m.forward.date.isoformat() if m.forward.date else None,
            "from_id": _ser_peer(m.forward.from_id) if m.forward.from_id else None,
            "from_name": m.forward.from_name,
        }
    return out


def _ser_peer(peer: Any) -> Dict[str, Any]:
    return {k: getattr(peer, k) for k in ("user_id", "chat_id", "channel_id") if hasattr(peer, k) and getattr(peer, k) is not None}


async def export_account(
    client: TelegramClient,
    out_dir: Path,
    options: ExportOptions,
    progress: ProgressCb,
) -> Path:
    """Выполняет полный экспорт в out_dir. Возвращает путь к out_dir."""
    out_dir.mkdir(parents=True, exist_ok=True)

    await progress(1, "запрос профиля")
    me = await client.get_me()
    profile = {
        "id": me.id,
        "first_name": me.first_name,
        "last_name": me.last_name,
        "username": me.username,
        "phone": me.phone,
        "is_premium": bool(getattr(me, "premium", False)),
        "lang_code": getattr(me, "lang_code", None),
        "exported_at_utc": datetime.now(timezone.utc).isoformat(),
        "tool": "tde-agent",
    }
    (out_dir / "profile.json").write_text(json.dumps(profile, ensure_ascii=False, indent=2), encoding="utf-8")

    await progress(3, "запрос контактов")
    try:
        contacts = await client.get_contacts()
        contacts_data = [{
            "id": c.id, "first_name": c.first_name, "last_name": c.last_name,
            "username": c.username, "phone": c.phone,
        } for c in contacts]
    except Exception:
        contacts_data = []
    (out_dir / "contacts.json").write_text(json.dumps(contacts_data, ensure_ascii=False, indent=2), encoding="utf-8")

    await progress(5, "сбор списка диалогов")
    dialogs = []
    async for d in client.iter_dialogs():
        dialogs.append(d)

    dialogs_meta: List[Dict[str, Any]] = []
    chats_root = out_dir / "chats"
    chats_root.mkdir(exist_ok=True)

    total = max(1, len(dialogs))
    for i, dialog in enumerate(dialogs, 1):
        entity = dialog.entity
        ent_info = _serialize_entity(entity)
        slug = _slug(dialog.name)
        chat_folder = chats_root / f"{abs(dialog.id)}_{slug}"
        chat_folder.mkdir(exist_ok=True)

        chat_meta: Dict[str, Any] = {
            "dialog_id": dialog.id,
            "name": dialog.name,
            "entity": ent_info,
            "unread_count": dialog.unread_count,
            "pinned": dialog.pinned,
            "archived": dialog.archived,
            "folder": chat_folder.name,
            "message_count": 0,
            "media_count": 0,
        }

        if options.include_messages:
            try:
                messages, media_count = await _dump_chat(
                    client, dialog, chat_folder, options, progress,
                    chat_index=i, chat_total=total,
                )
                chat_meta["message_count"] = len(messages)
                chat_meta["media_count"] = media_count
                # messages.json
                (chat_folder / "messages.json").write_text(
                    json.dumps(messages, ensure_ascii=False, indent=2), encoding="utf-8"
                )
                # messages.html
                (chat_folder / "messages.html").write_text(
                    render_chat(chat_meta, messages), encoding="utf-8"
                )
            except Exception as e:
                (chat_folder / "error.txt").write_text(f"cannot export: {type(e).__name__}: {e}", encoding="utf-8")
                chat_meta["error"] = f"{type(e).__name__}: {e}"

        dialogs_meta.append(chat_meta)
        pct = 5 + int(90 * i / total)
        await progress(pct, f"диалог {i}/{total}: {dialog.name[:40]}")

    (out_dir / "dialogs.json").write_text(json.dumps(dialogs_meta, ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / "index.html").write_text(render_index(profile, dialogs_meta), encoding="utf-8")

    # README в формате оригинального экспорта
    (out_dir / "README.md").write_text(_render_readme(profile, dialogs_meta, options), encoding="utf-8")

    await progress(96, "экспорт завершён")
    return out_dir


async def _dump_chat(
    client: TelegramClient,
    dialog,
    folder: Path,
    opts: ExportOptions,
    progress: ProgressCb,
    chat_index: int,
    chat_total: int,
) -> tuple[List[Dict[str, Any]], int]:
    """Выкачивает сообщения одного диалога + медиа по категориям."""
    messages: List[Dict[str, Any]] = []
    media_count = 0
    msg_iter_kwargs: Dict[str, Any] = {}
    if opts.message_limit_per_chat > 0:
        msg_iter_kwargs["limit"] = opts.message_limit_per_chat
    if opts.date_from:
        msg_iter_kwargs["offset_date"] = opts.date_to or datetime.now(timezone.utc)

    async for m in client.iter_messages(dialog.entity, **msg_iter_kwargs):
        if opts.date_from and m.date and m.date < opts.date_from:
            break
        saved: Optional[str] = None
        if opts.include_media and m.media is not None:
            sub, ext = _classify_media(m)
            # фильтр по размеру
            size = _media_size(m)
            if size and size > opts.media_max_bytes_per_file:
                saved = None  # пропускаем, пишем только метаданные
            else:
                try:
                    subdir = folder / sub
                    subdir.mkdir(exist_ok=True)
                    fname = _doc_filename(m, ext)
                    out = subdir / f"{m.id}_{_slug(fname, 60)}"
                    path = await client.download_media(m, file=str(out))
                    if path:
                        saved = os.path.relpath(path, folder)
                        media_count += 1
                except Exception:
                    saved = None
        messages.append(_msg_to_dict(m, saved))

    # хронологический порядок
    messages.reverse()
    return messages, media_count


def _media_size(m: Message) -> Optional[int]:
    media = m.media
    if isinstance(media, MessageMediaDocument) and media.document is not None:
        return media.document.size
    if isinstance(media, MessageMediaPhoto) and media.photo is not None:
        sizes = getattr(media.photo, "sizes", None) or []
        if sizes:
            try:
                return max((getattr(s, "size", 0) or 0) for s in sizes)
            except Exception:
                return None
    return None


def _render_readme(profile: Dict[str, Any], dialogs: List[Dict[str, Any]], opts: ExportOptions) -> str:
    total_msgs = sum(d.get("message_count", 0) for d in dialogs)
    total_media = sum(d.get("media_count", 0) for d in dialogs)
    return f"""# Telegram Data Export

**Пользователь:** {profile.get('first_name') or ''} {profile.get('last_name') or ''} (@{profile.get('username') or '—'})
**ID:** {profile['id']}
**Дата экспорта:** {profile['exported_at_utc']}

## Статистика

| Метрика | Значение |
|---|---|
| Диалогов | {len(dialogs)} |
| Сообщений | {total_msgs} |
| Медиафайлов | {total_media} |
| Сообщения включены | {"да" if opts.include_messages else "нет"} |
| Медиа включено | {"да" if opts.include_media else "нет"} |
| Лимит сообщений на диалог | {"без лимита" if opts.message_limit_per_chat == 0 else opts.message_limit_per_chat} |
| Пропущены файлы > | {opts.media_max_bytes_per_file // (1024 * 1024)} MB |

## Структура

```
.
├── profile.json         # данные профиля
├── contacts.json        # контакты адресной книги
├── dialogs.json         # сводная таблица диалогов
├── index.html           # главная страница
└── chats/
    └── <id>_<name>/
        ├── messages.json
        ├── messages.html
        ├── photos/
        ├── video_files/
        ├── voice_messages/
        ├── round_video_messages/
        ├── files/
        ├── stickers/
        └── animations/
```

Формат совместим со структурой Telegram Desktop Export.

## Как открыть

Откройте `index.html` в браузере — получите интерактивный просмотр
с навигацией по диалогам, фото-галереей, встроенным аудиоплеером
для голосовых и видеоплеером для круглых сообщений.
"""
