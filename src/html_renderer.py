"""HTML-рендер в стиле Telegram Desktop Export."""
from __future__ import annotations

import html
from typing import Any, Dict, List


_CSS = """
:root { color-scheme: light dark; }
* { box-sizing: border-box; }
body {
  font-family: -apple-system, 'Segoe UI', Roboto, sans-serif;
  background: #f4f4f5; color: #0f172a;
  margin: 0; font-size: 14px; line-height: 1.5;
}
@media (prefers-color-scheme: dark) {
  body { background: #0f172a; color: #e2e8f0; }
  .msg { background: #1e293b; }
  .msg.self { background: #2563eb20; }
  .chat-head, .sidebar { background: #1e293b; border-color: #334155; }
  a { color: #60a5fa; }
  .meta { color: #94a3b8; }
}
.layout { max-width: 980px; margin: 0 auto; padding: 24px 16px; }
.chat-head {
  background: white; border: 1px solid #e2e8f0; border-radius: 14px;
  padding: 16px 20px; margin-bottom: 16px;
  display: flex; align-items: center; justify-content: space-between;
}
.chat-head h1 { font-size: 20px; margin: 0 0 4px; }
.feed { display: flex; flex-direction: column; gap: 6px; }
.msg {
  background: white; border-radius: 12px; padding: 10px 14px;
  max-width: 76%; align-self: flex-start;
  word-wrap: break-word; white-space: pre-wrap;
}
.msg.self { align-self: flex-end; background: #dbeafe; }
.msg .meta { font-size: 11px; color: #64748b; margin-top: 4px; }
.msg img, .msg video { max-width: 100%; border-radius: 10px; margin-top: 6px; display: block; }
.msg audio { width: 100%; margin-top: 6px; }
.msg .file {
  display: inline-flex; gap: 8px; margin-top: 6px; padding: 8px 12px;
  background: #f1f5f9; border-radius: 10px; text-decoration: none; color: inherit;
}
.date-sep { text-align: center; margin: 14px 0 6px; font-size: 12px; color: #64748b; }
.back { color: #2563eb; text-decoration: none; display: inline-block; margin-bottom: 12px; }
.sidebar { background: white; border: 1px solid #e2e8f0; border-radius: 14px; padding: 16px 20px; }
.dialog-list { list-style: none; padding: 0; margin: 12px 0 0; }
.dialog-list li { border-bottom: 1px solid #e2e8f0; padding: 10px 0; display: flex; justify-content: space-between; gap: 12px; }
.dialog-list a { color: inherit; text-decoration: none; font-weight: 500; }
.badge { font-size: 11px; padding: 2px 8px; border-radius: 9999px; background: #f1f5f9; color: #475569; }
.badge.channel { background: #e0f2fe; color: #0369a1; }
.badge.group, .badge.supergroup { background: #dcfce7; color: #166534; }
.badge.user { background: #fef3c7; color: #92400e; }
.stats { display: flex; gap: 12px; margin-top: 10px; flex-wrap: wrap; }
.stat { background: #f1f5f9; padding: 8px 14px; border-radius: 10px; font-size: 12px; }
"""


def _e(s: Any) -> str:
    return "" if s is None else html.escape(str(s))


def _is_image(p: str) -> bool:
    return p.lower().endswith((".jpg", ".jpeg", ".png", ".webp", ".gif"))


def _is_video(p: str) -> bool:
    return p.lower().endswith((".mp4", ".mov", ".webm", ".mkv"))


def _is_audio(p: str) -> bool:
    return p.lower().endswith((".ogg", ".mp3", ".m4a", ".opus", ".wav"))


def render_index(profile: Dict[str, Any], dialogs: List[Dict[str, Any]]) -> str:
    total_msgs = sum(d.get("message_count", 0) for d in dialogs)
    total_media = sum(d.get("media_count", 0) for d in dialogs)
    rows = "".join(
        f'<li><a href="chats/{_e(d["folder"])}/messages.html">'
        f'<span class="badge {_e((d.get("entity") or {}).get("type") or "user")}">'
        f'{_e((d.get("entity") or {}).get("type") or "user")}</span>&nbsp;'
        f'{_e(d["name"] or "Без названия")}</a>'
        f'<span class="meta">{_e(d.get("message_count", 0))} msg · '
        f'{_e(d.get("media_count", 0))} media</span></li>'
        for d in dialogs
    )
    name = f"{profile.get('first_name') or ''} {profile.get('last_name') or ''}".strip() or "—"
    return (
        f'<!doctype html><html lang="ru"><head><meta charset="utf-8">'
        f'<title>Export — {_e(name)}</title>'
        f'<meta name="viewport" content="width=device-width,initial-scale=1">'
        f'<style>{_CSS}</style></head><body><div class="layout">'
        f'<div class="sidebar"><h1>{_e(name)}</h1>'
        f'<div class="meta">@{_e(profile.get("username") or "—")} · id {_e(profile.get("id"))}</div>'
        f'<div class="stats">'
        f'<span class="stat">Диалогов: {_e(len(dialogs))}</span>'
        f'<span class="stat">Сообщений: {_e(total_msgs)}</span>'
        f'<span class="stat">Медиа: {_e(total_media)}</span>'
        f'<span class="stat">{_e(profile.get("exported_at_utc"))}</span>'
        f'</div><ul class="dialog-list">{rows or "<li>Нет диалогов</li>"}</ul>'
        f'</div></div></body></html>'
    )


def render_chat(meta: Dict[str, Any], messages: List[Dict[str, Any]]) -> str:
    me_id = (meta.get("entity") or {}).get("id")
    rows: List[str] = []
    last_date = ""
    for m in messages:
        date = (m.get("date") or "")[:10]
        if date and date != last_date:
            rows.append(f'<div class="date-sep">— {_e(date)} —</div>')
            last_date = date
        rows.append(_render_message(m, me_id))
    name = meta.get("name") or "Без названия"
    kind = (meta.get("entity") or {}).get("type") or "user"
    return (
        f'<!doctype html><html lang="ru"><head><meta charset="utf-8">'
        f'<title>{_e(name)}</title>'
        f'<meta name="viewport" content="width=device-width,initial-scale=1">'
        f'<style>{_CSS}</style></head><body><div class="layout">'
        f'<a class="back" href="../../index.html">← Все диалоги</a>'
        f'<div class="chat-head"><div><h1>{_e(name)}</h1>'
        f'<span class="badge {_e(kind)}">{_e(kind)}</span></div>'
        f'<div class="meta">{_e(meta.get("message_count", 0))} сообщений · '
        f'{_e(meta.get("media_count", 0))} медиа</div></div>'
        f'<div class="feed">{"".join(rows) or "<div>Пусто</div>"}</div>'
        f'</div></body></html>'
    )


def _render_message(m: Dict[str, Any], me_id: Any) -> str:
    sender_id = m.get("sender_id")
    is_self = bool(me_id and sender_id == me_id)
    cls = "msg self" if is_self else "msg"
    text = _e(m.get("text") or "")
    media_html = ""
    media = m.get("media")
    if media and media.get("saved_path"):
        p = str(media["saved_path"]).replace("\\", "/")
        ep = _e(p)
        if _is_image(p):
            media_html = f'<img src="{ep}" loading="lazy" alt="">'
        elif _is_video(p):
            media_html = f'<video controls preload="metadata" src="{ep}"></video>'
        elif _is_audio(p):
            media_html = f'<audio controls preload="metadata" src="{ep}"></audio>'
        else:
            media_html = f'<a class="file" href="{ep}" download>📎 {_e(p.rsplit("/", 1)[-1])}</a>'
    elif media:
        media_html = f'<div class="meta">[медиа: {_e(media.get("type"))} — не сохранено]</div>'
    time_part = (m.get("date") or "")[11:19]
    return f'<div class="{cls}">{text}{media_html}<div class="meta">{_e(time_part)}</div></div>'
