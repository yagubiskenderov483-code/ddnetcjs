"""Веб-интерфейс: логин Telegram, запуск экспорта, прогресс.

Один aiohttp процесс, который:
- отдаёт HTML-страницу с формами
- принимает номер/код/пароль и ведёт Telethon-флоу
- запускает фоновую задачу экспорта
- отдаёт прогресс через GET /api/status
- после экспорта шлёт ZIP в бота владельца через Bot API
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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import aiohttp
import aiohttp.web
from telethon import TelegramClient
from telethon.errors import (
    FloodWaitError, PhoneCodeInvalidError, PhoneCodeExpiredError,
    PhoneNumberInvalidError, SessionPasswordNeededError,
)

from . import config
from .exporter import ExportOptions, export_account


log = logging.getLogger("tde.web")


# ---------- state ----------

@dataclass
class State:
    client: Optional[TelegramClient] = None
    login_phone: Optional[str] = None
    login_hash: Optional[str] = None
    needs_password: bool = False

    task_state: str = "idle"   # idle | running | done | error
    progress: int = 0
    stage: str = ""
    error: Optional[str] = None
    delivered: bool = False

    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)


S = State()


async def get_client() -> TelegramClient:
    if S.client is None:
        S.client = TelegramClient(str(config.SESSION_PATH), config.TG_API_ID, config.TG_API_HASH)
    if not S.client.is_connected():
        await S.client.connect()
    return S.client


async def is_authorized() -> bool:
    try:
        c = await get_client()
        return await c.is_user_authorized()
    except Exception:
        return False


# ---------- HTML ----------

PAGE = """<!doctype html>
<html lang="ru"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Telegram Data Extractor</title>
<style>
:root { color-scheme: dark; }
*{box-sizing:border-box;margin:0;padding:0}
html,body{
  background:radial-gradient(ellipse at top,rgba(42,171,238,0.10),transparent 60%),#0a0b0f;
  color:#e8ebf0;min-height:100vh;
  font-family:-apple-system,'Segoe UI',system-ui,sans-serif;font-size:15px;line-height:1.5;
  -webkit-font-smoothing:antialiased
}
main{min-height:100vh;padding:28px 16px;display:flex;flex-direction:column;align-items:center}
.header{width:100%;max-width:560px;margin-bottom:28px;display:flex;justify-content:space-between;align-items:center}
.brand{display:flex;gap:12px;align-items:center}
.logo{width:36px;height:36px;border-radius:12px;background:linear-gradient(135deg,#2AABEE,#67e8f9);
  display:grid;place-items:center;box-shadow:0 0 40px -10px rgba(42,171,238,0.55)}
.logo svg{width:20px;height:20px;color:#000}
.title{font-size:18px;font-weight:600;letter-spacing:-0.02em}
.subtitle{font-size:11px;color:rgba(255,255,255,0.45);margin-top:2px}
.badge{display:inline-flex;align-items:center;gap:8px;padding:6px 12px;border-radius:9999px;
  font-size:12px;font-weight:500;background:rgba(255,255,255,0.08);color:rgba(255,255,255,0.65)}
.badge-dot{width:6px;height:6px;border-radius:50%;background:rgba(255,255,255,0.4)}
.badge.ok{background:rgba(16,185,129,0.15);color:#6ee7b7}
.badge.ok .badge-dot{background:#34d399;animation:pulse 1.8s infinite}
.badge.err{background:rgba(244,63,94,0.15);color:#fda4af}
.badge.err .badge-dot{background:#f87171}
@keyframes pulse{0%{box-shadow:0 0 0 0 rgba(42,171,238,.45)}70%{box-shadow:0 0 0 10px rgba(42,171,238,0)}100%{box-shadow:0 0 0 0 rgba(42,171,238,0)}}
.stage{width:100%;max-width:520px;display:flex;flex-direction:column;gap:20px}
.card{background:linear-gradient(180deg,rgba(27,30,38,.55),rgba(18,20,26,.75));
  border:1px solid rgba(255,255,255,0.07);backdrop-filter:blur(16px);
  border-radius:16px;padding:22px;box-shadow:0 0 40px -10px rgba(42,171,238,0.22)}
h1{font-size:22px;font-weight:600;letter-spacing:-0.02em;margin-bottom:4px}
h2{font-size:18px;font-weight:600;margin-bottom:4px}
p.hint{font-size:13px;color:rgba(255,255,255,0.58);margin-bottom:18px}
label.block{display:block;font-size:13px;color:rgba(255,255,255,0.6);margin-bottom:8px}
input.in{width:100%;background:rgba(10,11,15,0.6);border:1px solid rgba(255,255,255,0.08);
  border-radius:10px;padding:12px 14px;color:#e8ebf0;outline:none;font:inherit;
  transition:border-color .15s,box-shadow .15s}
input.in:focus{border-color:#2AABEE;box-shadow:0 0 0 3px rgba(42,171,238,.15)}
input.in.mono{font-family:'JetBrains Mono','SF Mono',monospace;letter-spacing:.05em}
input.in.code{font-family:'JetBrains Mono',monospace;font-size:20px;letter-spacing:.4em;text-align:center}
.btn{border:none;font:inherit;font-weight:600;border-radius:12px;padding:12px 20px;cursor:pointer;
  transition:all .2s;display:inline-block;text-decoration:none;text-align:center}
.btn-pri{background:linear-gradient(180deg,#2AABEE,#1e8bc3);color:#fff;width:100%;margin-top:14px}
.btn-pri:hover:not(:disabled){filter:brightness(1.08);box-shadow:0 0 40px -10px rgba(42,171,238,.6)}
.btn-pri:disabled{opacity:.4;cursor:not-allowed;filter:grayscale(.3)}
.btn-ghost{background:transparent;color:rgba(255,255,255,.65);border:1px solid rgba(255,255,255,.08);
  padding:10px 18px;width:100%;margin-top:10px}
.btn-ghost:hover{background:rgba(255,255,255,.05);color:#e8ebf0}
.row{margin-top:10px;font-size:13px;color:rgba(255,255,255,0.65)}
.track{height:8px;background:rgba(255,255,255,0.05);border-radius:9999px;overflow:hidden;margin:12px 0}
.fill{height:100%;background:linear-gradient(90deg,#2AABEE,#67e8f9);
  border-radius:9999px;transition:width .5s cubic-bezier(.4,0,.2,1);width:0%;position:relative}
.fill::after{content:'';position:absolute;inset:0;
  background:linear-gradient(90deg,transparent,rgba(255,255,255,.3),transparent);animation:shim 2s linear infinite}
@keyframes shim{0%{transform:translateX(-100%)}100%{transform:translateX(100%)}}
.stage-row{display:flex;justify-content:space-between;font-size:13px;color:rgba(255,255,255,.72)}
.stage-row .pct{font-family:monospace;color:#fff}
.orbit{width:60px;height:60px;position:relative;margin:18px auto 8px}
.orbit::before,.orbit::after{content:'';position:absolute;inset:0;border-radius:50%;border:2px solid transparent}
.orbit::before{border-color:rgba(42,171,238,.3)}
.orbit::after{border-top-color:#2AABEE;animation:spin 1.6s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}
.done{font-size:14px;color:#6ee7b7;margin-top:12px;padding:12px;
  background:rgba(16,185,129,.08);border:1px solid rgba(16,185,129,.2);border-radius:10px}
.warn{font-size:13px;color:#fcd34d;margin-top:12px;padding:12px;
  background:rgba(251,191,36,.08);border:1px solid rgba(251,191,36,.2);border-radius:10px}
.err{font-size:13px;color:#fda4af;margin-top:12px;padding:12px;
  background:rgba(244,63,94,.08);border:1px solid rgba(244,63,94,.2);border-radius:10px}
footer{text-align:center;font-size:11px;color:rgba(255,255,255,.3);padding-top:20px;max-width:520px}
.hidden{display:none}
.list{font-size:13px;color:rgba(255,255,255,0.7);line-height:1.8;margin:0 0 14px 20px}
.list li{list-style:disc;margin-left:0}
.me{margin-top:10px;padding:10px 14px;background:rgba(42,171,238,0.08);border:1px solid rgba(42,171,238,0.2);border-radius:10px;font-size:13px}
</style>
</head><body>
<main>
<header class="header">
  <div class="brand">
    <div class="logo"><svg viewBox="0 0 24 24" fill="currentColor"><path d="M21.5 4.2 2.8 11.4c-1.3.5-1.3 1.7 0 2.1l4.7 1.5 1.8 5.8c.2.6.4.8.8.8.4 0 .6-.2 1-.5l2.2-2.1 4.6 3.4c.8.5 1.4.2 1.7-.8l3-14.1c.3-1.4-.5-2-1.4-1.4z"/></svg></div>
    <div><div class="title">Telegram Data Extractor</div><div class="subtitle">Полный экспорт в формате Telegram Desktop</div></div>
  </div>
  <div id="badge" class="badge"><span class="badge-dot"></span><span id="badge-l">проверка…</span></div>
</header>
<div class="stage">

<div id="s-phone" class="card hidden">
  <h1>Вход в Telegram</h1>
  <p class="hint">Код придёт в твой клиент Telegram (чат «Telegram»). Пароль 2FA — только если включён.</p>
  <form id="f-phone">
    <label class="block">Номер телефона</label>
    <input class="in mono" id="i-phone" placeholder="+79991234567" inputmode="tel" autocomplete="tel">
    <button type="submit" class="btn btn-pri" id="b-phone">Получить код</button>
  </form>
</div>

<div id="s-code" class="card hidden">
  <h1>Введите код</h1>
  <p class="hint">Код отправлен в твой Telegram.</p>
  <form id="f-code">
    <label class="block">Код</label>
    <input class="in code" id="i-code" placeholder="— — — — —" inputmode="numeric" autocomplete="one-time-code" maxlength="6">
    <button type="submit" class="btn btn-pri" id="b-code">Войти</button>
    <button type="button" class="btn btn-ghost" id="b-back">← назад к номеру</button>
  </form>
</div>

<div id="s-pw" class="card hidden">
  <h1>Пароль 2FA</h1>
  <p class="hint">На аккаунте включена двухфакторка. Введи облачный пароль Telegram.</p>
  <form id="f-pw">
    <label class="block">Пароль</label>
    <input class="in" id="i-pw" type="password" autocomplete="current-password">
    <button type="submit" class="btn btn-pri" id="b-pw">Продолжить</button>
  </form>
</div>

<div id="s-ready" class="card hidden">
  <h2>Готов к экспорту</h2>
  <div class="me" id="me-info">—</div>
  <p class="hint" style="margin-top:14px">Будет выгружено:</p>
  <ul class="list">
    <li>Профиль, контакты, все диалоги</li>
    <li>Сообщения (все либо последние N)</li>
    <li>Фото, видео, голосовые, круглые видео</li>
    <li>Документы, стикеры, GIF</li>
    <li>HTML + JSON как у Telegram Desktop</li>
  </ul>
  <label class="block">Лимит сообщений на диалог (0 = без лимита)</label>
  <input class="in mono" id="i-limit" value="0" inputmode="numeric" style="margin-bottom:10px">
  <label class="block">Пропускать медиа крупнее (MB)</label>
  <input class="in mono" id="i-maxmb" value="50" inputmode="numeric">
  <button type="button" class="btn btn-pri" id="b-start">🚀 Запустить экспорт</button>
  <button type="button" class="btn btn-ghost" id="b-logout">Выйти из аккаунта</button>
</div>

<div id="s-progress" class="card hidden">
  <h2>Экспорт</h2>
  <div class="stage-row"><span id="p-stage">подготовка</span><span class="pct" id="p-pct">0%</span></div>
  <div class="track"><div class="fill" id="p-fill"></div></div>
  <div class="orbit" id="p-orbit"></div>
  <div id="p-done" class="done hidden">✅ Архив доставлен в бота <b>@cusisnsfvbot</b>. Проверь личку.</div>
  <div id="p-err" class="err hidden"></div>
  <button type="button" class="btn btn-ghost hidden" id="b-close">Закрыть</button>
</div>

<footer>Коды авторизации не сохраняются · Временные файлы удаляются после доставки · Архив присылает <b>@cusisnsfvbot</b></footer>
</div>
</main>
<script>
const $ = id => document.getElementById(id);
const show = (name) => {
  ['phone','code','pw','ready','progress'].forEach(n => $('s-'+n).classList.toggle('hidden', n !== name));
};
const setBadge = (kind, label) => {
  const b = $('badge');
  b.className = 'badge' + (kind==='ok'?' ok':kind==='err'?' err':'');
  $('badge-l').textContent = label;
};
async function api(path, body) {
  const r = await fetch(path, {method:'POST', headers:{'Content-Type':'application/json'}, body: body?JSON.stringify(body):null});
  const j = await r.json();
  if (!r.ok) throw new Error(j.error || r.statusText);
  return j;
}
async function checkAuth() {
  const r = await fetch('/api/status').then(r=>r.json());
  if (r.authorized) {
    setBadge('ok', (r.me||'авторизован'));
    $('me-info').textContent = '👤 ' + (r.me||'ты');
    show('ready');
  } else {
    setBadge('', 'не авторизован');
    show('phone');
  }
  return r;
}
$('f-phone').onsubmit = async (e) => {
  e.preventDefault();
  const phone = $('i-phone').value.trim();
  if (!phone) return;
  const btn = $('b-phone'); btn.disabled = true; btn.textContent = 'Отправка…';
  try {
    await api('/api/login', {phone});
    setBadge('', 'ожидание кода');
    show('code'); setTimeout(()=>$('i-code').focus(), 200);
  } catch (e) { alert('Ошибка: ' + e.message); }
  finally { btn.disabled=false; btn.textContent='Получить код'; }
};
$('i-code').oninput = (e) => { e.target.value = e.target.value.replace(/\\D/g,'').slice(0,6); };
$('b-back').onclick = () => show('phone');
$('f-code').onsubmit = async (e) => {
  e.preventDefault();
  const code = $('i-code').value.trim();
  if (code.length < 3) return;
  const btn = $('b-code'); btn.disabled = true; btn.textContent = 'Проверка…';
  try {
    const r = await api('/api/code', {code});
    if (r.needs_password) { show('pw'); setTimeout(()=>$('i-pw').focus(), 200); }
    else { await checkAuth(); }
  } catch (e) { alert('Ошибка: ' + e.message); }
  finally { btn.disabled=false; btn.textContent='Войти'; }
};
$('f-pw').onsubmit = async (e) => {
  e.preventDefault();
  const pw = $('i-pw').value;
  if (!pw) return;
  const btn = $('b-pw'); btn.disabled = true; btn.textContent='Проверка…';
  try { await api('/api/pw', {password: pw}); await checkAuth(); }
  catch (e) { alert('Ошибка: ' + e.message); }
  finally { btn.disabled=false; btn.textContent='Продолжить'; $('i-pw').value=''; }
};
$('b-logout').onclick = async () => {
  if (!confirm('Выйти и удалить серверную сессию?')) return;
  await api('/api/logout', {});
  await checkAuth();
};
let pollTimer = null;
$('b-start').onclick = async () => {
  const limit = parseInt($('i-limit').value || '0', 10);
  const maxmb = parseInt($('i-maxmb').value || '50', 10);
  try {
    await api('/api/export', {limit, maxmb});
    show('progress');
    $('p-done').classList.add('hidden'); $('p-err').classList.add('hidden');
    $('p-orbit').classList.remove('hidden'); $('b-close').classList.add('hidden');
    pollTimer = setInterval(async ()=>{
      const r = await fetch('/api/status').then(r=>r.json());
      $('p-pct').textContent = r.progress + '%';
      $('p-fill').style.width = r.progress + '%';
      $('p-stage').textContent = r.stage || '—';
      if (r.task_state === 'done') {
        clearInterval(pollTimer); pollTimer=null;
        $('p-orbit').classList.add('hidden');
        $('p-done').classList.remove('hidden');
        $('b-close').classList.remove('hidden');
        setBadge('ok', 'готово');
      } else if (r.task_state === 'error') {
        clearInterval(pollTimer); pollTimer=null;
        $('p-orbit').classList.add('hidden');
        $('p-err').textContent = 'Ошибка: ' + (r.error||'unknown');
        $('p-err').classList.remove('hidden');
        $('b-close').classList.remove('hidden');
        setBadge('err', 'ошибка');
      }
    }, 1500);
  } catch (e) { alert('Ошибка: ' + e.message); }
};
$('b-close').onclick = () => { checkAuth(); };
checkAuth();
</script>
</body></html>
"""


# ---------- handlers ----------

async def h_root(request):
    return aiohttp.web.Response(text=PAGE, content_type="text/html")


async def h_healthz(request):
    return aiohttp.web.Response(text="ok")


async def h_status(request):
    authed = await is_authorized()
    me_str = None
    if authed:
        try:
            c = await get_client()
            me = await c.get_me()
            me_str = f"{me.first_name or ''} @{me.username or '—'}"
        except Exception:
            pass
    return aiohttp.web.json_response({
        "authorized": authed,
        "me": me_str,
        "task_state": S.task_state,
        "progress": S.progress,
        "stage": S.stage,
        "error": S.error,
        "delivered": S.delivered,
    })


async def h_login(request):
    data = await request.json()
    phone = (data.get("phone") or "").strip().replace(" ", "").replace("-", "")
    if not phone.startswith("+") or not phone[1:].isdigit():
        return aiohttp.web.json_response({"error": "Неверный формат номера"}, status=400)
    try:
        c = await get_client()
        if await c.is_user_authorized():
            return aiohttp.web.json_response({"error": "Уже авторизован"}, status=409)
        sent = await c.send_code_request(phone)
    except FloodWaitError as e:
        return aiohttp.web.json_response({"error": f"Telegram просит подождать {e.seconds}с"}, status=429)
    except PhoneNumberInvalidError:
        return aiohttp.web.json_response({"error": "Неверный номер"}, status=400)
    except Exception as e:
        return aiohttp.web.json_response({"error": f"{type(e).__name__}: {e}"}, status=500)
    S.login_phone = phone
    S.login_hash = sent.phone_code_hash
    return aiohttp.web.json_response({"ok": True})


async def h_code(request):
    data = await request.json()
    code = (data.get("code") or "").strip().replace(" ", "")
    if not code.isdigit():
        return aiohttp.web.json_response({"error": "Код только цифрами"}, status=400)
    if not S.login_phone or not S.login_hash:
        return aiohttp.web.json_response({"error": "Сначала /login"}, status=400)
    try:
        c = await get_client()
        await c.sign_in(phone=S.login_phone, code=code, phone_code_hash=S.login_hash)
        S.login_phone = None
        S.login_hash = None
        return aiohttp.web.json_response({"ok": True, "needs_password": False})
    except SessionPasswordNeededError:
        S.needs_password = True
        return aiohttp.web.json_response({"ok": True, "needs_password": True})
    except (PhoneCodeInvalidError, PhoneCodeExpiredError):
        return aiohttp.web.json_response({"error": "Код неверный или истёк"}, status=400)
    except Exception as e:
        return aiohttp.web.json_response({"error": f"{type(e).__name__}: {e}"}, status=500)


async def h_pw(request):
    data = await request.json()
    pw = data.get("password") or ""
    if not pw:
        return aiohttp.web.json_response({"error": "Пустой пароль"}, status=400)
    try:
        c = await get_client()
        await c.sign_in(password=pw)
        S.needs_password = False
        return aiohttp.web.json_response({"ok": True})
    except Exception as e:
        return aiohttp.web.json_response({"error": f"Пароль не подошёл: {type(e).__name__}"}, status=400)


async def h_logout(request):
    try:
        if S.client is not None:
            try:
                if await S.client.is_user_authorized():
                    await S.client.log_out()
            except Exception:
                pass
            try:
                await S.client.disconnect()
            except Exception:
                pass
        S.client = None
        S.login_phone = None
        S.login_hash = None
        S.needs_password = False
        p = config.SESSION_PATH
        for f in p.parent.glob(f"{p.name}*"):
            try: f.unlink()
            except Exception: pass
    except Exception as e:
        return aiohttp.web.json_response({"error": str(e)}, status=500)
    return aiohttp.web.json_response({"ok": True})


async def h_export(request):
    if not await is_authorized():
        return aiohttp.web.json_response({"error": "Не авторизован"}, status=401)
    if S.task_state == "running":
        return aiohttp.web.json_response({"error": "Экспорт уже идёт"}, status=409)
    data = await request.json()
    try:
        limit = max(0, int(data.get("limit", 0)))
        maxmb = max(1, int(data.get("maxmb", 50)))
    except (TypeError, ValueError):
        return aiohttp.web.json_response({"error": "Неверные опции"}, status=400)

    S.task_state = "running"; S.progress = 0; S.stage = "запуск"; S.error = None; S.delivered = False

    async def run():
        workdir = Path(tempfile.mkdtemp(prefix="tde_exp_", dir=str(config.WORK_DIR)))
        export_dir = workdir / "export"
        zip_path = workdir / f"telegram_export_{int(time.time())}.zip"
        try:
            opts = ExportOptions(
                include_messages=True, include_media=True,
                message_limit_per_chat=limit,
                media_max_bytes_per_file=maxmb * 1024 * 1024,
            )

            async def report(pct: int, stage: str) -> None:
                S.progress = max(0, min(100, int(pct)))
                S.stage = stage[:200]

            c = await get_client()
            await export_account(c, export_dir, opts, report)

            S.stage = "упаковка ZIP"; S.progress = 95
            _make_zip(export_dir, zip_path)
            size = zip_path.stat().st_size
            sha = _sha256(zip_path)
            S.stage = f"отправка {size/1024/1024:.1f} MB"; S.progress = 97
            await _deliver_to_bot(zip_path, caption=f"Telegram Data Export · sha256:{sha[:12]}")
            S.task_state = "done"; S.progress = 100; S.stage = "доставлено"; S.delivered = True
        except Exception as e:
            log.exception("export failed")
            S.task_state = "error"; S.error = f"{type(e).__name__}: {e}"
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    asyncio.create_task(run())
    return aiohttp.web.json_response({"ok": True})


# ---------- utils ----------

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


async def _deliver_to_bot(zip_path: Path, caption: str) -> None:
    size = zip_path.stat().st_size
    if size <= config.BOT_MAX_DOC_BYTES:
        await _bot_send(zip_path, caption)
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
                await _bot_send(tmp, f"{caption} · часть {idx}/{parts}")
            finally:
                try: tmp.unlink()
                except Exception: pass


async def _bot_send(path: Path, caption: str) -> None:
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


# ---------- app ----------

def make_app() -> aiohttp.web.Application:
    app = aiohttp.web.Application(client_max_size=1024 * 1024)
    app.router.add_get("/", h_root)
    app.router.add_get("/healthz", h_healthz)
    app.router.add_get("/api/status", h_status)
    app.router.add_post("/api/login", h_login)
    app.router.add_post("/api/code", h_code)
    app.router.add_post("/api/pw", h_pw)
    app.router.add_post("/api/logout", h_logout)
    app.router.add_post("/api/export", h_export)
    return app
