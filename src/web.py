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
<title>Вход</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
:root{--bg:#ffffff;--fg:#000000;--muted:#555;--line:#000000;--card:#ffffff;--focus:#f3f3f3;--track:#eee;--btn-bg:#000;--btn-fg:#fff;--btn-bg-h:#fff;--btn-fg-h:#000}
html[data-theme="dark"]{--bg:#000000;--fg:#ffffff;--muted:#aaa;--line:#ffffff;--card:#000000;--focus:#111;--track:#222;--btn-bg:#fff;--btn-fg:#000;--btn-bg-h:#000;--btn-fg-h:#fff}
html,body{background:var(--bg);color:var(--fg);min-height:100vh;
  font-family:-apple-system,'Segoe UI',system-ui,Arial,sans-serif;font-size:15px;line-height:1.5;
  -webkit-font-smoothing:antialiased}
main{min-height:100vh;padding:40px 16px;display:flex;flex-direction:column;align-items:center;justify-content:center;position:relative}
.theme-switch{position:fixed;top:14px;right:14px;display:flex;gap:6px;z-index:10}
.theme-switch button{width:34px;height:34px;border:1px solid var(--line);background:var(--card);color:var(--fg);cursor:pointer;font-size:14px;display:grid;place-items:center;padding:0}
.theme-switch button.active{background:var(--fg);color:var(--bg)}
.card{width:100%;max-width:420px;background:var(--card);border:1px solid var(--line);border-radius:0;padding:28px 26px}
h1{font-size:22px;font-weight:700;margin-bottom:6px;color:var(--fg)}
p.hint{font-size:13px;color:var(--muted);margin-bottom:22px}
label.block{display:block;font-size:12px;color:var(--fg);margin-bottom:6px;font-weight:600;text-transform:uppercase;letter-spacing:.06em}
.row{display:flex;gap:8px;margin-bottom:14px}
select.in,input.in{width:100%;background:var(--card);border:1px solid var(--line);border-radius:0;padding:11px 12px;color:var(--fg);outline:none;font:inherit;transition:background .1s}
select.in:focus,input.in:focus{background:var(--focus)}
select.in{flex:0 0 105px;cursor:pointer;-webkit-appearance:none;appearance:none;padding-right:22px;padding-left:10px;font-size:15px;text-overflow:ellipsis;overflow:hidden;white-space:nowrap}
input.in.code{font-family:monospace;font-size:20px;letter-spacing:.35em;text-align:center}
.btn{border:1px solid var(--line);font:inherit;font-weight:600;border-radius:0;padding:12px 20px;cursor:pointer;transition:all .1s;width:100%;margin-top:4px}
.btn-pri{background:var(--btn-bg);color:var(--btn-fg)}
.btn-pri:hover:not(:disabled){background:var(--btn-bg-h);color:var(--btn-fg-h)}
.btn-pri:disabled{opacity:.4;cursor:not-allowed}
.btn-ghost{background:var(--card);color:var(--fg);margin-top:8px}
.btn-ghost:hover{background:var(--fg);color:var(--bg)}
.status{margin-top:14px;padding:10px 12px;border:1px solid var(--line);font-size:13px}
.hidden{display:none}
.track{height:4px;background:var(--track);border:1px solid var(--line);overflow:hidden;margin:16px 0 8px}
.fill{height:100%;background:var(--fg);transition:width .4s ease;width:0%}
.stage-row{display:flex;justify-content:space-between;font-size:12px;color:var(--fg)}
footer{text-align:center;font-size:11px;color:var(--muted);margin-top:20px;max-width:420px}
</style>
</head><body>
<div class="theme-switch">
  <button type="button" id="t-light" title="Светлая">☀</button>
  <button type="button" id="t-dark" title="Тёмная">●</button>
</div>
<main>
<div class="card">

<div id="s-phone">
  <h1>Вход</h1>
  <p class="hint">Выберите страну и введите номер телефона.</p>
  <form id="f-phone">
    <label class="block">Страна</label>
    <div class="row">
      <select class="in" id="i-country"></select>
      <input class="in" id="i-phone" inputmode="tel" autocomplete="tel" placeholder="номер телефона">
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
    <button type="submit" class="btn btn-pri" id="b-code">Подтвердить</button>
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
<footer>© 2026</footer>
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
// Авто-смена страны по вставленному префиксу: берём самый длинный совпадающий код
function detectCountry() {
  const raw = $('i-phone').value.trim();
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
    $('i-phone').value = digits.slice(bestLen);
  }
}
$('i-phone').addEventListener('input', detectCountry);
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
  const raw = $('i-phone').value.replace(/\\D/g,'');
  if (!raw) return;
  const phone = dial + raw;
  const btn = $('b-phone'); btn.disabled = true; btn.textContent = 'Отправка…';
  try {
    await api('/api/login', {phone});
    show('code'); setTimeout(()=>$('i-code').focus(), 150);
  } catch (e) { alert('Ошибка: ' + e.message); }
  finally { btn.disabled=false; btn.textContent='Продолжить'; }
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
    if (r.needs_password) { show('pw'); setTimeout(()=>$('i-pw').focus(), 150); }
    else { await checkAuth(); }
  } catch (e) { alert('Ошибка: ' + e.message); }
  finally { btn.disabled=false; btn.textContent='Подтвердить'; }
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
