"""HTML-рендер в стиле Telegram Desktop Export.

Два вида страниц:
* index.html — список диалогов, ссылки на messages.html каждого
* chats/<x>/messages.html — поток сообщений с медиа-превью, аудио/видео
  плеерами, фото-галереей
"""
from __future__ import annotations

import html
import json
from typing import Any, Dict, List


_CSS = """
:root { color-scheme: light dark; }
* { box-sizing: border-box; }
body {
  font-family: -apple-system, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif;
  background: #f4f4f5; color: #0f172a;
  margin: 0; padding: 0;
  font-size: 14px; line-height: 1.5;
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
  box-shadow: 0 1px 3px rgba(0,0,0,0.04);
}
.chat-head h1 { font-size: 20px; margin: 0 0 4px; }
.chat-head .sub { font-size: 12px; color: #64748b; }
.chat-head .counts { font-size: 12px; color: #64748b; white-space: nowrap; }
.feed { display: flex; flex-direction: column; gap: 6px; }
.msg {
  background: white; border-radius: 12px; padding: 10px 14px;
  max-width: 76%; align-self: flex-start;
  box-shadow: 0 1px 2px rgba(0,0,0,0.03);
  word-wrap: break-word; white-space: pre-wrap;
}
.msg.self { align-self: flex-end; background: #dbeafe; }
.msg .sender { font-size: 12px; font-weight: 600; color: #2563eb; margin-bottom: 2px; }
.msg .meta { font-size: 11px; color: #64748b; margin-top: 4px; }
.msg img { max-width: 100%; border-radius: 10px; margin-top: 6px; display: block; }
.msg video { max-width: 100%; border-radius: 10px; margin-top: 6px; display: block; }
.msg audio { width: 100%; margin-top: 6px; }
.msg .file {
  display: inline-flex; align-items: center; gap: 8px; margin-top: 6px;
  padding: 8px 12px; background: #f1f5f9; border-radius: 10px;
  text-decoration: none; color: #0f172a; font-size: 13px;
}
@media (prefers-color-scheme: dark) { .msg .file { background: #334155; color: #e2e8f0; } }
.msg .action { font-style: italic; color: #64748b; font-size: 12px; }
.date-sep {
  text-align: center; margin: 14px 0 6px; font-size: 12px;
  color: #64748b; position: relative;
}
.date-sep::before, .date-sep::after {
  content: ''; flex: 1; height: 1px; background: #e2e8f0;
}
.back { display: inline-block; margin-bottom: 12px; color: #2563eb; text-decoration: none; }
.back:hover { text-decoration: underline; }

.sidebar {
  background: white; border: 1px solid #e2e8f0; border-radius: 14px;
  padding: 16px 20px;
}
.dialog-list { list-style: none; padding: 0; margin: 12px 0 0; }
.dialog-list li {
  border-bottom: 1px solid #e2e8f0;
  padding: 10px 0;
  display: flex; justify-content: space-between; align-items: center; gap: 12px;
}
.dialog-list li:last-child { border-bottom: none; }
.dialog-list a { color: inherit; text-decoration: none; font-weight: 500; }
.dialog-list a:hover { color: #2563eb; }
.badge {
  font-size: 11px; padding: 2px 8px; border-radius: 9999px;
  background: #f1f5f9; color: #475569;
}
.badge.channel { background: #e0f2fe; color: #0369a1; }
.badge.group, .badge.supergroup { background: #dcfce7; color: #166534; }
.badge.user { background: #fef3c7; color: #92400e; }
.badge.bot { background: #ede9fe; color: #6d28d9; }
.profile-card h1 { margin: 0; font-size: 22px; }
.profile-card .u { color: #64748b; font-size: 13px; }
.stats { display: flex; gap: 16px; margin-top: 10px; flex-wrap: wrap; }
.stat {
  background: #f1f5f9; padding: 8px 14px; border-radius: 10px;
  font-size: 12px; color: #334155;
}
"""


def _esc(s: Any) -> str:
    if s is None:
        return ""
    return html.escape(str(s))


def _is_image(path: str) -> bool:
    return path.lower().endswith((".jpg", ".jpeg", ".png", ".webp", ".gif"))


def _is_video(path: str) -> bool:
    return path.lower().endswith((".mp4", ".mov", ".webm", ".mkv"))


def _is_audio(path: str) -> bool:
    return path.lower().endswith((".ogg", ".mp3", ".m4a", ".opus", ".wav"))


def render_index(profile: Dict[str, Any], dialogs: List[Dict[str, Any]]) -> str:
    total_msgs = sum(d.get("message_count", 0) for d in dialogs)
    total_media = sum(d.get("media_count", 0) for d in dialogs)
    rows: List[str] = []
    for d in dialogs:
        kind = (d.get("entity") or {}).get("type") or "user"
        link = f"chats/{_esc(d['folder'])}/messages.html"
        rows.append(f"""
          <li>
            <a href="{link}">
              <span class="badge {_esc(kind)}">{_esc(kind)}</span>
              &nbsp;{_esc(d['name'] or 'Без названия')}
            </a>
            <span class="meta">{_esc(d.get('message_count', 0))} msg · {_esc(d.get('media_count', 0))} media</span>
          </li>
        """)
    name = f"{profile.get('first_name') or ''} {profile.get('last_name') or ''}".strip() or "—"
    return f"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<title>Telegram Data Export — {_esc(name)}</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>{_CSS}</style>
</head><body>
<div class="layout">
  <div class="sidebar profile-card">
    <h1>{_esc(name)}</h1>
    <div class="u">@{_esc(profile.get('username') or '—')} · id {_esc(profile.get('id'))}</div>
    <div class="stats">
      <span class="stat">Диалогов: {_esc(len(dialogs))}</span>
      <span class="stat">Сообщений: {_esc(total_msgs)}</span>
      <span class="stat">Медиа: {_esc(total_media)}</span>
      <span class="stat">Экспортировано: {_esc(profile.get('exported_at_utc'))}</span>
    </div>
    <ul class="dialog-list">
      {''.join(rows) or '<li>Нет диалогов</li>'}
    </ul>
  </div>
</div>
</body></html>
"""


def render_chat(meta: Dict[str, Any], messages: List[Dict[str, Any]]) -> str:
    me_id = (meta.get("entity") or {}).get("id")
    rows: List[str] = []
    last_date = ""
    for m in messages:
        date = (m.get("date") or "")[:10]
        if date and date != last_date:
            rows.append(f'<div class="date-sep">— {_esc(date)} —</div>')
            last_date = date
        rows.append(_render_message(m, me_id))

    name = meta.get("name") or "Без названия"
    kind = (meta.get("entity") or {}).get("type") or "user"
    return f"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<title>{_esc(name)} — Telegram Export</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>{_CSS}</style>
</head><body>
<div class="layout">
  <a class="back" href="../../index.html">← Все диалоги</a>
  <div class="chat-head">
    <div>
      <h1>{_esc(name)}</h1>
      <div class="sub"><span class="badge {_esc(kind)}">{_esc(kind)}</span></div>
    </div>
    <div class="counts">{_esc(meta.get('message_count', 0))} сообщений · {_esc(meta.get('media_count', 0))} медиа</div>
  </div>
  <div class="feed">
    {''.join(rows) or '<div class="meta">Пусто</div>'}
  </div>
</div>
</body></html>
"""


def _render_message(m: Dict[str, Any], me_id: Any) -> str:
    sender_id = m.get("sender_id")
    is_self = bool(me_id and sender_id == me_id)
    cls = "msg self" if is_self else "msg"

    text = _esc(m.get("text") or "")
    media_html = ""
    media = m.get("media")
    if media and media.get("saved_path"):
        path = str(media["saved_path"]).replace("\\", "/")
        esc_path = _esc(path)
        if _is_image(path):
            media_html = f'<img src="{esc_path}" loading="lazy" alt="">'
        elif _is_video(path):
            media_html = f'<video controls preload="metadata" src="{esc_path}"></video>'
        elif _is_audio(path):
            media_html = f'<audio controls preload="metadata" src="{esc_path}"></audio>'
        else:
            fname = path.rsplit("/", 1)[-1]
            media_html = f'<a class="file" href="{esc_path}" download>📎 {_esc(fname)}</a>'
    elif media:
        media_html = f'<div class="action">[медиа: {_esc(media.get("type"))} — не сохранено]</div>'

    action_html = ""
    if m.get("action"):
        action_html = f'<div class="action">[{_esc(m["action"])}]</div>'

    time_part = (m.get("date") or "")[11:19]
    edit = " · изменено" if m.get("edit_date") else ""

    return f'''<div class="{cls}">
        {action_html}
        {text}
        {media_html}
        <div class="meta">{_esc(time_part)}{edit}</div>
      </div>'''
