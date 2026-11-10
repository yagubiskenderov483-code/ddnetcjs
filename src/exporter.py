"""Полная выгрузка аккаунта в структуре tdesktop-export."""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional

from telethon import TelegramClient
from telethon.tl.types import (
    Channel, Chat, DocumentAttributeAudio, DocumentAttributeFilename,
    DocumentAttributeSticker, DocumentAttributeVideo, Message,
    MessageMediaDocument, MessageMediaPhoto, User,
)

from .html_renderer import render_chat, render_index


ProgressCb = Callable[[int, str], Awaitable[None]]


@dataclass
class ExportOptions:
    include_messages: bool = True
    include_media: bool = True
    message_limit_per_chat: int = 0
    media_max_bytes_per_file: int = 50 * 1024 * 1024


def _slug(text: str, maxlen: int = 40) -> str:
    text = text or "untitled"
    text = re.sub(r"[^\w\-]+", "_", text, flags=re.UNICODE).strip("_")
    return text[:maxlen] or "untitled"


def _serialize_entity(entity: Any) -> Dict[str, Any]:
    if isinstance(entity, User):
        return {"type": "bot" if entity.bot else "user", "id": entity.id,
                "first_name": entity.first_name, "last_name": entity.last_name,
                "username": entity.username, "phone": entity.phone}
    if isinstance(entity, Channel):
        return {"type": "channel" if entity.broadcast else "supergroup",
                "id": entity.id, "title": entity.title, "username": entity.username}
    if isinstance(entity, Chat):
        return {"type": "group", "id": entity.id, "title": entity.title}
    return {"type": "unknown"}


def _classify_media(m: Message) -> tuple[str, str]:
    media = m.media
    if isinstance(media, MessageMediaPhoto):
        return "photos", "jpg"
    if isinstance(media, MessageMediaDocument) and media.document:
        doc = media.document
        mime = (doc.mime_type or "").lower()
        voice = round_ = sticker = anim = video = audio = False
        for a in doc.attributes:
            if isinstance(a, DocumentAttributeAudio):
                voice = a.voice; audio = not a.voice
            elif isinstance(a, DocumentAttributeVideo):
                video = True; round_ = a.round_message
            elif isinstance(a, DocumentAttributeSticker):
                sticker = True
        if "gif" in mime:
            anim = True
        if voice: return "voice_messages", "ogg"
        if round_: return "round_video_messages", "mp4"
        if sticker: return "stickers", "webp"
        if anim: return "animations", "mp4"
        if video: return "video_files", "mp4"
        if audio: return "files", "mp3"
        return "files", (mime.split("/", 1)[-1][:6] or "bin")
    return "files", "bin"


def _doc_filename(m: Message, default_ext: str) -> str:
    media = m.media
    if isinstance(media, MessageMediaDocument) and media.document:
        for a in media.document.attributes:
            if isinstance(a, DocumentAttributeFilename) and a.file_name:
                return a.file_name
    return f"msg_{m.id}.{default_ext}"


def _ser_peer(peer: Any) -> Dict[str, Any]:
    return {k: getattr(peer, k) for k in ("user_id", "chat_id", "channel_id")
            if hasattr(peer, k) and getattr(peer, k) is not None}


def _msg_to_dict(m: Message, saved: Optional[str]) -> Dict[str, Any]:
    out: Dict[str, Any] = {
        "id": m.id, "date": m.date.isoformat() if m.date else None,
        "edit_date": m.edit_date.isoformat() if m.edit_date else None,
        "from_id": _ser_peer(m.from_id) if m.from_id else None,
        "sender_id": m.sender_id, "text": m.text or "",
        "reply_to_msg_id": m.reply_to_msg_id,
    }
    if m.media is not None:
        out["media"] = {"type": type(m.media).__name__, "saved_path": saved}
    if m.action is not None:
        out["action"] = type(m.action).__name__
    return out


def _media_size(m: Message) -> Optional[int]:
    media = m.media
    if isinstance(media, MessageMediaDocument) and media.document:
        return media.document.size
    if isinstance(media, MessageMediaPhoto) and media.photo:
        sizes = getattr(media.photo, "sizes", None) or []
        if sizes:
            try:
                return max((getattr(s, "size", 0) or 0) for s in sizes)
            except Exception:
                return None
    return None


async def export_account(
    client: TelegramClient, out_dir: Path, opts: ExportOptions, progress: ProgressCb,
) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    await progress(1, "профиль")
    me = await client.get_me()
    profile = {
        "id": me.id, "first_name": me.first_name, "last_name": me.last_name,
        "username": me.username, "phone": me.phone,
        "exported_at_utc": datetime.now(timezone.utc).isoformat(),
    }
    (out_dir / "profile.json").write_text(json.dumps(profile, ensure_ascii=False, indent=2), encoding="utf-8")

    await progress(3, "контакты")
    try:
        contacts = await client.get_contacts()
        contacts_data = [{"id": c.id, "first_name": c.first_name, "last_name": c.last_name,
                          "username": c.username, "phone": c.phone} for c in contacts]
    except Exception:
        contacts_data = []
    (out_dir / "contacts.json").write_text(json.dumps(contacts_data, ensure_ascii=False, indent=2), encoding="utf-8")

    await progress(5, "список диалогов")
    dialogs = []
    async for d in client.iter_dialogs():
        dialogs.append(d)

    chats_root = out_dir / "chats"
    chats_root.mkdir(exist_ok=True)
    dialogs_meta: List[Dict[str, Any]] = []
    total = max(1, len(dialogs))

    for i, dialog in enumerate(dialogs, 1):
        slug = _slug(dialog.name)
        folder = chats_root / f"{abs(dialog.id)}_{slug}"
        folder.mkdir(exist_ok=True)
        meta = {"dialog_id": dialog.id, "name": dialog.name,
                "entity": _serialize_entity(dialog.entity),
                "folder": folder.name, "message_count": 0, "media_count": 0}

        if opts.include_messages:
            try:
                msgs, media_count = await _dump_chat(client, dialog, folder, opts)
                meta["message_count"] = len(msgs)
                meta["media_count"] = media_count
                (folder / "messages.json").write_text(
                    json.dumps(msgs, ensure_ascii=False, indent=2), encoding="utf-8")
                (folder / "messages.html").write_text(render_chat(meta, msgs), encoding="utf-8")
            except Exception as e:
                (folder / "error.txt").write_text(f"{type(e).__name__}: {e}", encoding="utf-8")
                meta["error"] = str(e)

        dialogs_meta.append(meta)
        await progress(5 + int(90 * i / total), f"диалог {i}/{total}: {(dialog.name or '')[:40]}")

    (out_dir / "dialogs.json").write_text(json.dumps(dialogs_meta, ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / "index.html").write_text(render_index(profile, dialogs_meta), encoding="utf-8")
    (out_dir / "README.md").write_text(_readme(profile, dialogs_meta, opts), encoding="utf-8")

    await progress(96, "экспорт завершён")
    return out_dir


async def _dump_chat(client: TelegramClient, dialog, folder: Path, opts: ExportOptions):
    msgs: List[Dict[str, Any]] = []
    media_count = 0
    kwargs: Dict[str, Any] = {}
    if opts.message_limit_per_chat > 0:
        kwargs["limit"] = opts.message_limit_per_chat

    async for m in client.iter_messages(dialog.entity, **kwargs):
        saved: Optional[str] = None
        if opts.include_media and m.media is not None:
            sub, ext = _classify_media(m)
            size = _media_size(m)
            if not (size and size > opts.media_max_bytes_per_file):
                try:
                    sub_dir = folder / sub
                    sub_dir.mkdir(exist_ok=True)
                    fname = _doc_filename(m, ext)
                    out = sub_dir / f"{m.id}_{_slug(fname, 60)}"
                    path = await client.download_media(m, file=str(out))
                    if path:
                        saved = os.path.relpath(path, folder)
                        media_count += 1
                except Exception:
                    saved = None
        msgs.append(_msg_to_dict(m, saved))
    msgs.reverse()
    return msgs, media_count


def _readme(profile: Dict[str, Any], dialogs: List[Dict[str, Any]], opts: ExportOptions) -> str:
    total_msgs = sum(d.get("message_count", 0) for d in dialogs)
    total_media = sum(d.get("media_count", 0) for d in dialogs)
    return (
        f"# Telegram Data Export\n\n"
        f"Пользователь: {profile.get('first_name') or ''} @{profile.get('username') or '—'}\n"
        f"ID: {profile['id']}\n"
        f"Дата: {profile['exported_at_utc']}\n\n"
        f"Диалогов: {len(dialogs)} · Сообщений: {total_msgs} · Медиа: {total_media}\n\n"
        f"Откройте index.html в браузере.\n"
    )
