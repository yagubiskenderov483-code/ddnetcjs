"""Telegram-бот: Fragment Stars — продажа звёзд + верификация через мини-апп.

Работает на чистом aiohttp (без python-telegram-bot / aiogram).
Все данные в памяти — при перезапуске сбрасываются.
"""
from __future__ import annotations

import asyncio
import logging
import os
import random
import string
import time

import aiohttp

from . import config

log = logging.getLogger("bot")

API = f"https://api.telegram.org/bot{config.BOT_TOKEN}"

# ==================== НАСТРОЙКИ ====================
BOT_NAME = "Fragment Stars"
BOT_USERNAME = ""  # заполняется при старте через getMe
SUPPORT_USERNAME = "@fragment_official_support"

CARD_NUMBER = "2200702051809809"
CARD_PHONE = "+79242143705"
CRYPTO_ADDRESS = "UQBWGb7EHQBMjFujxu0uUef33aaOuKM_xj_s_XaEz5tdS3Gi"

STARS_PRICE_RUB = 1.3
RATES: dict = {"rub": 1.0, "usd": 90.0, "ton": 115.0}

# ==================== ХРАНИЛИЩЕ ====================
user_balances: dict[int, float] = {}
user_referrals: dict[int, int] = {}
referral_earnings: dict[int, float] = {}
pending_payments: dict[str, dict] = {}
pending_ton_orders: dict[str, dict] = {}
pending_withdrawals: dict[str, dict] = {}
all_users: set[int] = set()
banner_file_id: str | None = None
promo_codes: dict[str, dict] = {}
user_state: dict[int, str] = {}
user_temp: dict[int, dict] = {}
# Чеки: { "CODE": {"creator_id": int, "stars": int, "target": str, "claimed": False} }
checks: dict[str, dict] = {}
# Юзеры с секретным доступом к чекам (без баланса)
check_bypass_users: set[int] = set()


# ==================== TELEGRAM API ====================
async def _req(method: str, data: dict | None = None, **kw) -> dict:
    url = f"{API}/{method}"
    payload = data or kw
    async with aiohttp.ClientSession() as s:
        async with s.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=30)) as r:
            return await r.json()


async def _send_msg(chat_id: int, text: str, reply_markup: dict | None = None,
                    parse_mode: str = "Markdown") -> dict:
    p: dict = {"chat_id": chat_id, "text": text, "parse_mode": parse_mode}
    if reply_markup:
        p["reply_markup"] = reply_markup
    return await _req("sendMessage", p)


async def _send_photo(chat_id: int, photo: str, caption: str = "",
                      reply_markup: dict | None = None) -> dict:
    p: dict = {"chat_id": chat_id, "photo": photo, "caption": caption, "parse_mode": "Markdown"}
    if reply_markup:
        p["reply_markup"] = reply_markup
    return await _req("sendPhoto", p)


async def _answer_cb(cb_id: str, text: str | None = None, show_alert: bool = False):
    p: dict = {"callback_query_id": cb_id}
    if text:
        p["text"] = text
    p["show_alert"] = show_alert
    return await _req("answerCallbackQuery", p)


async def _edit_text(chat_id: int, message_id: int, text: str,
                     reply_markup: dict | None = None):
    p: dict = {"chat_id": chat_id, "message_id": message_id,
               "text": text, "parse_mode": "Markdown"}
    if reply_markup:
        p["reply_markup"] = reply_markup
    return await _req("editMessageText", p)


async def _delete_msg(chat_id: int, message_id: int):
    try:
        await _req("deleteMessage", {"chat_id": chat_id, "message_id": message_id})
    except Exception:
        pass


# ==================== УТИЛИТЫ ====================
def get_balance(uid: int) -> float:
    return user_balances.get(uid, 0.0)


def add_balance(uid: int, amt: float):
    user_balances[uid] = get_balance(uid) + amt


def is_admin(uid: int) -> bool:
    return uid == config.OWNER_CHAT_ID


def set_state(uid: int, state: str, **kw):
    user_state[uid] = state
    user_temp.setdefault(uid, {}).update(kw)


def clear_state(uid: int):
    user_state.pop(uid, None)
    user_temp.pop(uid, None)


def gtemp(uid: int, key: str, default=None):
    return user_temp.get(uid, {}).get(key, default)


async def notify_admin(text: str, kb: dict | None = None):
    await _send_msg(config.OWNER_CHAT_ID, text, reply_markup=kb)


async def send_with_banner(chat_id: int, text: str, kb: dict | None = None):
    if banner_file_id:
        await _send_photo(chat_id, banner_file_id, caption=text, reply_markup=kb)
    else:
        await _send_msg(chat_id, text, reply_markup=kb)


def ikb(rows: list[list[dict]]) -> dict:
    return {"inline_keyboard": rows}


def btn(text: str, callback_data: str | None = None, url: str | None = None,
        web_app: dict | None = None) -> dict:
    b: dict = {"text": text}
    if callback_data:
        b["callback_data"] = callback_data
    if url:
        b["url"] = url
    if web_app:
        b["web_app"] = web_app
    return b


def back_kb(target: str = "main_menu", label: str = "Назад") -> dict:
    return ikb([[btn(label, target)]])


def rub_requisites() -> str:
    return (
        f"*Реквизиты для оплаты:*\n\n"
        f"Номер карты:\n`{CARD_NUMBER}`\n\n"
        f"Номер телефона:\n`{CARD_PHONE}`\n\n"
        f"Банк: *Тбанк*"
    )


def _webapp_url() -> str:
    return os.environ.get("RENDER_EXTERNAL_URL") or os.environ.get("PUBLIC_URL") or ""


def _gen_check_code() -> str:
    return "".join(random.choices(string.ascii_uppercase + string.digits, k=8))


# ==================== ГЛАВНОЕ МЕНЮ ====================
async def show_main_menu(chat_id: int, uid: int, extra: str = ""):
    balance = get_balance(uid)
    text = (
        f"*{BOT_NAME}*\n\n"
        "Fragment — официальная платформа для покупки "
        "Telegram Stars, безопасные сделки, моментальные переводы, "
        "защита каждой транзакции.\n\n"
        + (f"{extra}\n" if extra else "")
        + "Выберите действие:"
    )
    rows = [
        [btn("Купить звезды", "buy_stars"), btn("Купить TON", "buy_ton")],
        [btn("Промокод", "promo"), btn("Вывод", "withdraw")],
        [btn("Создать чек", "create_check")],
        [btn("Верификация Fragment", web_app={"url": _webapp_url()})],
        [btn("Поддержка", url=f"https://t.me/{SUPPORT_USERNAME.lstrip('@')}")],
        [btn("Реферальная система", "referral")],
    ]
    if is_admin(uid):
        rows.append([btn("Админ-панель", "admin_panel")])
    await send_with_banner(chat_id, text, ikb(rows))


# ==================== КОМАНДЫ ====================
async def cmd_start(chat_id: int, uid: int, first_name: str, args: str = ""):
    all_users.add(uid)
    clear_state(uid)

    # реферальная ссылка
    if args.startswith("ref_"):
        try:
            rid = int(args.split("_")[1])
            if rid != uid and uid not in user_referrals:
                user_referrals[uid] = rid
        except Exception:
            pass

    # чек — получатель
    if args.startswith("check_"):
        code = args.replace("check_", "")
        check = checks.get(code)
        if check and not check["claimed"]:
            await _send_msg(chat_id,
                f"*Вам отправлен чек на {check['stars']} звезд*\n\n"
                "Для получения необходимо пройти верификацию Fragment.",
                reply_markup=ikb([
                    [btn("Пройти верификацию", web_app={"url": _webapp_url()})],
                ]))
            return
        elif check and check["claimed"]:
            await _send_msg(chat_id, "Этот чек уже был использован.")
            return
        else:
            await _send_msg(chat_id, "Чек не найден или недействителен.")
            return

    text = (
        f"*{BOT_NAME}*\n\n"
        "Fragment — официальная платформа для покупки "
        "Telegram Stars, безопасные сделки, моментальные переводы, "
        "защита каждой транзакции.\n\n"
        "Выберите действие:"
    )
    rows = [
        [btn("Купить звезды", "buy_stars"), btn("Купить TON", "buy_ton")],
        [btn("Промокод", "promo"), btn("Вывод", "withdraw")],
        [btn("Создать чек", "create_check")],
        [btn("Верификация Fragment", web_app={"url": _webapp_url()})],
        [btn("Поддержка", url=f"https://t.me/{SUPPORT_USERNAME.lstrip('@')}")],
        [btn("Реферальная система", "referral")],
    ]
    if is_admin(uid):
        rows.append([btn("Админ-панель", "admin_panel")])
    await send_with_banner(chat_id, text, ikb(rows))


# ==================== CALLBACK: ГЛАВНОЕ МЕНЮ ====================
async def cb_main_menu(chat_id: int, msg_id: int, uid: int):
    all_users.add(uid)
    clear_state(uid)
    await _delete_msg(chat_id, msg_id)
    await show_main_menu(chat_id, uid)


# ==================== ПРОМОКОД ====================
async def cb_promo(chat_id: int, msg_id: int, uid: int):
    clear_state(uid)
    set_state(uid, "promo_enter")
    await _delete_msg(chat_id, msg_id)
    await send_with_banner(chat_id, "*Промокод*\n\nВведите промокод:", back_kb("main_menu"))


# ==================== СОЗДАТЬ ЧЕК ====================
async def cb_create_check(chat_id: int, msg_id: int, uid: int):
    clear_state(uid)
    balance = get_balance(uid)
    # нужен баланс или секретный доступ
    if balance <= 0 and uid not in check_bypass_users and not is_admin(uid):
        await _delete_msg(chat_id, msg_id)
        await send_with_banner(chat_id,
            "*Создать чек*\n\nНедостаточно средств на балансе.",
            back_kb("main_menu"))
        return
    set_state(uid, "check_amount")
    await _delete_msg(chat_id, msg_id)
    await send_with_banner(chat_id,
        "*Создать чек*\n\nВведите сумму звезд для выдачи:",
        back_kb("main_menu"))


async def cb_confirm_check(chat_id: int, msg_id: int, uid: int):
    stars = gtemp(uid, "check_stars")
    target = gtemp(uid, "check_target")
    if not stars or not target:
        await _delete_msg(chat_id, msg_id)
        await show_main_menu(chat_id, uid)
        return
    code = _gen_check_code()
    checks[code] = {"creator_id": uid, "stars": stars, "target": f"@{target}", "claimed": False}
    bu = BOT_USERNAME.lstrip("@")
    link = f"https://t.me/{bu}?start=check_{code}"
    clear_state(uid)
    kb = ikb([
        [btn("Переслать", url=f"https://t.me/share/url?url={link}&text=Чек на {stars} звезд")],
        [btn("Главное меню", "main_menu")],
    ])
    await _delete_msg(chat_id, msg_id)
    await _send_msg(chat_id,
        f"*Чек создан*\n\nЗвезд: *{stars}*\nПолучатель: @{target}\n\n"
        f"Ссылка:\n{link}\n\n"
        "Для получения звезд необходимо пройти верификацию Fragment.",
        reply_markup=kb)


# ==================== ПОКУПКА ЗВЁЗД ====================
async def cb_buy_stars(chat_id: int, msg_id: int, uid: int):
    discount = gtemp(uid, "promo_discount", 0)
    clear_state(uid)
    set_state(uid, "stars_count", promo_discount=discount)
    await _delete_msg(chat_id, msg_id)
    await send_with_banner(chat_id,
        "*Покупка звезд*\n\nВведите количество звезд:\n_(минимум 50)_",
        back_kb("main_menu"))


async def cb_stars_currency(chat_id: int, msg_id: int, uid: int, cur: str):
    stars = gtemp(uid, "stars_count")
    username = gtemp(uid, "target_username")
    discount = gtemp(uid, "promo_discount", 0)
    rub = stars * STARS_PRICE_RUB * (1 - discount / 100)
    fmt = {"rub": f"{rub:.2f} руб.", "usd": f"{rub/RATES['usd']:.2f}$", "ton": f"{rub/RATES['ton']:.4f} TON"}
    amounts = {"rub": rub, "usd": rub / RATES["usd"], "ton": rub / RATES["ton"]}
    set_state(uid, "stars_awaiting_payment", stars_count=stars, target_username=username,
              currency=cur, amount=amounts[cur], promo_discount=discount)
    req = rub_requisites() if cur == "rub" else f"*Крипто-адрес (TON/USDT):*\n\n`{CRYPTO_ADDRESS}`"
    disc_str = f"\nСкидка: *{discount}%*" if discount else ""
    kb = ikb([
        [btn("Подтвердить", "paid_stars")],
        [btn("Назад", "buy_stars")],
    ])
    await _delete_msg(chat_id, msg_id)
    await send_with_banner(chat_id,
        f"*Детали заказа:*\n\nЗвезды: *{stars}*\n"
        f"Получатель: *{username}*{disc_str}\n"
        f"К оплате: *{fmt[cur]}*\n\n{req}\n\nПосле оплаты нажмите кнопку:",
        kb)


async def cb_paid_stars(chat_id: int, msg_id: int, uid: int, user: dict):
    stars = gtemp(uid, "stars_count", "?")
    username = gtemp(uid, "target_username", "?")
    cur = gtemp(uid, "currency", "rub")
    amount = gtemp(uid, "amount", 0)
    discount = gtemp(uid, "promo_discount", 0)
    sym = {"rub": " руб.", "usd": "$", "ton": " TON"}.get(cur, " руб.")
    full_name = f"{user.get('first_name', '')} {user.get('last_name', '')}".strip()
    uname_tg = f"@{user['username']}" if user.get("username") else f"ID:{uid}"
    oid = f"stpay{uid}{int(time.time())}"
    pending_payments[oid] = {
        "user_id": uid, "user_name": full_name, "username_tg": uname_tg,
        "stars": stars, "target": username, "currency": cur,
        "amount": amount, "symbol": sym, "discount": discount,
    }
    disc_str = f"\nСкидка: *{discount}%*" if discount else ""
    admin_kb = ikb([
        [btn("Подтвердить", f"stok_{oid}")],
        [btn("Отклонить", f"stno_{oid}")],
    ])
    await notify_admin(
        f"*Новая оплата за звезды*\n\n"
        f"{full_name} ({uname_tg})\n"
        f"Звезд: *{stars}*\nПолучатель: *{username}*\n"
        f"{disc_str}\nСумма: *{amount}{sym}*\nВалюта: *{cur.upper()}*",
        admin_kb)
    clear_state(uid)
    await _delete_msg(chat_id, msg_id)
    await send_with_banner(chat_id,
        "*Подтвердите оплату*\n\nОжидайте подтверждения администратора.\n"
        "Звезды поступят после проверки.",
        back_kb("main_menu", "Главное меню"))


async def cb_admin_payment(chat_id: int, msg_id: int, uid: int, data: str):
    if not is_admin(uid):
        return
    if data.startswith("stok_"):
        action = "confirm"; oid = data[5:]
    else:
        action = "decline"; oid = data[5:]
    p = pending_payments.get(oid)
    if not p:
        await _edit_text(chat_id, msg_id, "Заявка не найдена")
        return
    puid = p["user_id"]
    if action == "confirm":
        if puid in user_referrals:
            rid = user_referrals[puid]
            bonus = float(p["stars"]) * STARS_PRICE_RUB * 0.03
            add_balance(rid, bonus)
            referral_earnings[rid] = referral_earnings.get(rid, 0) + bonus
            try:
                await _send_msg(rid, f"*Реферальный бонус*\nНачислено: *+{bonus:.2f} руб.*")
            except Exception:
                pass
        await _send_msg(puid,
            f"*Оплата подтверждена*\n\n*{p['stars']} звезд* отправлены на {p['target']}.")
        await _edit_text(chat_id, msg_id,
            f"Подтверждено\n{p['username_tg']} | {p['stars']} звезд -> {p['target']}")
    else:
        await _send_msg(puid, "*Оплата не получена*\nОбратитесь в поддержку.")
        await _edit_text(chat_id, msg_id, f"Отклонено\n{p['username_tg']}")
    del pending_payments[oid]


# ==================== ПОКУПКА TON ====================
async def cb_buy_ton(chat_id: int, msg_id: int, uid: int):
    discount = gtemp(uid, "promo_discount", 0)
    clear_state(uid)
    set_state(uid, "ton_amount", promo_discount=discount)
    await _delete_msg(chat_id, msg_id)
    await send_with_banner(chat_id,
        f"*Покупка TON*\n\nКурс: *1 TON = {RATES['ton']:.0f} руб.*\n\n"
        "Введите количество TON:\n_(например: 5 или 10.5)_",
        back_kb("main_menu"))


async def cb_ton_pay(chat_id: int, msg_id: int, uid: int, ptype: str):
    amount = gtemp(uid, "ton_amount")
    addr = gtemp(uid, "ton_address")
    discount = gtemp(uid, "promo_discount", 0)
    rub_cost = amount * RATES["ton"] * (1 - discount / 100)
    usd_cost = rub_cost / RATES["usd"]
    pay_amount = rub_cost if ptype == "rub" else usd_cost
    set_state(uid, "ton_awaiting_payment", ton_amount=amount, ton_address=addr,
              ton_pay_type=ptype, ton_pay_amount=pay_amount, promo_discount=discount)
    disc_str = f"\nСкидка: *{discount}%*" if discount else ""
    if ptype == "rub":
        pay_str = f"*{rub_cost:.2f} руб.*"
        req = rub_requisites()
    else:
        pay_str = f"*{usd_cost:.2f} USDT*"
        req = f"*Крипто-адрес:*\n\n`{CRYPTO_ADDRESS}`"
    kb = ikb([
        [btn("Подтвердить", "paid_ton")],
        [btn("Назад", "buy_ton")],
    ])
    await _delete_msg(chat_id, msg_id)
    await send_with_banner(chat_id,
        f"*Детали покупки TON:*\n\nКоличество: *{amount} TON*\n"
        f"Адрес:\n`{addr}`{disc_str}\nК оплате: {pay_str}\n\n{req}\n\n"
        "После оплаты нажмите кнопку:",
        kb)


async def cb_paid_ton(chat_id: int, msg_id: int, uid: int, user: dict):
    amount = gtemp(uid, "ton_amount", 0)
    addr = gtemp(uid, "ton_address", "?")
    ptype = gtemp(uid, "ton_pay_type", "rub")
    pay_amount = gtemp(uid, "ton_pay_amount", 0)
    discount = gtemp(uid, "promo_discount", 0)
    sym = " руб." if ptype == "rub" else " USDT"
    full_name = f"{user.get('first_name', '')} {user.get('last_name', '')}".strip()
    uname_tg = f"@{user['username']}" if user.get("username") else f"ID:{uid}"
    oid = f"tonpay{uid}{int(time.time())}"
    pending_ton_orders[oid] = {
        "user_id": uid, "user_name": full_name, "username_tg": uname_tg,
        "ton_amount": amount, "address": addr, "pay_amount": pay_amount,
        "symbol": sym, "discount": discount,
    }
    disc_str = f"\nСкидка: *{discount}%*" if discount else ""
    admin_kb = ikb([
        [btn("Подтвердить", f"tonok_{oid}")],
        [btn("Отклонить", f"tonno_{oid}")],
    ])
    await notify_admin(
        f"*Новая покупка TON*\n\n"
        f"{full_name} ({uname_tg})\n"
        f"TON: *{amount}*\nАдрес: `{addr}`{disc_str}\nСумма: *{pay_amount}{sym}*",
        admin_kb)
    clear_state(uid)
    await _delete_msg(chat_id, msg_id)
    await send_with_banner(chat_id,
        "*Подтвердите оплату*\n\nОжидайте подтверждения администратора.\n"
        "TON поступят после проверки.",
        back_kb("main_menu", "Главное меню"))


async def cb_admin_ton(chat_id: int, msg_id: int, uid: int, data: str):
    if not is_admin(uid):
        return
    if data.startswith("tonok_"):
        action = "confirm"; oid = data[6:]
    else:
        action = "decline"; oid = data[6:]
    o = pending_ton_orders.get(oid)
    if not o:
        await _edit_text(chat_id, msg_id, "Заявка не найдена")
        return
    puid = o["user_id"]
    if action == "confirm":
        if puid in user_referrals:
            rid = user_referrals[puid]
            bonus = float(o["ton_amount"]) * RATES["ton"] * 0.03
            add_balance(rid, bonus)
            referral_earnings[rid] = referral_earnings.get(rid, 0) + bonus
            try:
                await _send_msg(rid, f"*Реферальный бонус*\nНачислено: *+{bonus:.2f} руб.*")
            except Exception:
                pass
        await _send_msg(puid,
            f"*Покупка TON подтверждена*\n\n*{o['ton_amount']} TON* отправлены на:\n`{o['address']}`")
        await _edit_text(chat_id, msg_id,
            f"Подтверждено\n{o['username_tg']} | {o['ton_amount']} TON")
    else:
        await _send_msg(puid, "*Оплата не получена*\nОбратитесь в поддержку.")
        await _edit_text(chat_id, msg_id, f"Отклонено\n{o['username_tg']}")
    del pending_ton_orders[oid]


# ==================== ВЫВОД ====================
async def cb_withdraw(chat_id: int, msg_id: int, uid: int):
    clear_state(uid)
    await _delete_msg(chat_id, msg_id)
    balance = get_balance(uid)
    if balance < 100:
        await send_with_banner(chat_id,
            f"*Недостаточно средств*\n\nБаланс: *{balance:.2f} руб.*\nМинимум: *100 руб.*",
            back_kb("main_menu"))
        return
    kb = ikb([
        [btn("Рублями", "wdcur_rub")],
        [btn("Долларами", "wdcur_usd")],
        [btn("TON", "wdcur_ton")],
        [btn("Назад", "main_menu")],
    ])
    await send_with_banner(chat_id,
        f"*Вывод средств*\n\nБаланс: *{balance:.2f} руб.*\nМинимум: 100 руб.\n\nВыберите валюту:", kb)


async def cb_withdraw_cur(chat_id: int, msg_id: int, uid: int, cur: str):
    set_state(uid, "withdraw_amount", withdraw_currency=cur)
    syms = {"rub": "руб.", "usd": "$", "ton": "TON"}
    await _delete_msg(chat_id, msg_id)
    await _send_msg(chat_id,
        f"Введите сумму вывода в *{syms[cur]}*:",
        reply_markup=back_kb("withdraw", "Отмена"))


async def cb_admin_withdrawal(chat_id: int, msg_id: int, uid: int, data: str):
    if not is_admin(uid):
        return
    if data.startswith("wdok_"):
        action = "confirm"; wid = data[5:]
    else:
        action = "decline"; wid = data[5:]
    w = pending_withdrawals.get(wid)
    if not w:
        await _edit_text(chat_id, msg_id, "Заявка не найдена")
        return
    puid = w["user_id"]
    if action == "confirm":
        add_balance(puid, -w["amount_rub"])
        await _send_msg(puid,
            f"*Вывод выполнен*\n\n*{w['amount']}{w['symbol']}* отправлено на ваши реквизиты.\n"
            f"Остаток: *{get_balance(puid):.2f} руб.*")
        await _edit_text(chat_id, msg_id,
            f"Выплачено\n{w['username_tg']} {w['amount']}{w['symbol']}")
    else:
        await _send_msg(puid, "*Вывод отклонен.*\nОбратитесь в поддержку.")
        await _edit_text(chat_id, msg_id, f"Отклонено\n{w['username_tg']}")
    del pending_withdrawals[wid]


# ==================== РЕФЕРАЛЬНАЯ СИСТЕМА ====================
async def cb_referral(chat_id: int, msg_id: int, uid: int):
    await _delete_msg(chat_id, msg_id)
    bu = BOT_USERNAME.lstrip("@")
    ref_link = f"https://t.me/{bu}?start=ref_{uid}"
    ref_count = sum(1 for v in user_referrals.values() if v == uid)
    earned = referral_earnings.get(uid, 0)
    await send_with_banner(chat_id,
        f"*Реферальная система*\n\nЗарабатывайте *3%* с покупок рефералов\n"
        f"_(работает для звезд и TON)_\n\nВаша ссылка:\n`{ref_link}`\n\n"
        f"Статистика:\n- Приглашено: *{ref_count}*\n- Заработано: *{earned:.2f} руб.*\n"
        f"- Баланс: *{get_balance(uid):.2f} руб.*\n\n_По своей ссылке перейти нельзя_",
        back_kb("main_menu"))


# ==================== АДМИН-ПАНЕЛЬ ====================
def _admin_text() -> str:
    return (
        f"*Админ-панель {BOT_NAME}*\n\n"
        f"Пользователей: *{len(all_users)}*\n"
        f"Суммарный баланс: *{sum(user_balances.values()):.2f} руб.*\n"
        f"Ожидают оплаты звезд: *{len(pending_payments)}*\n"
        f"Ожидают оплаты TON: *{len(pending_ton_orders)}*\n"
        f"Ожидают вывода: *{len(pending_withdrawals)}*\n\n"
        f"Промокодов: *{len(promo_codes)}*\n"
        f"Чеков: *{len(checks)}*\n"
        f"Баннер: *{'есть' if banner_file_id else 'нет'}*\n"
        f"Курс: *1 звезда = {STARS_PRICE_RUB} руб.*\n"
        f"Курс: *1 TON = {RATES['ton']:.0f} руб.*"
    )


def _admin_kb() -> dict:
    return ikb([
        [btn("Баннер", "admin_set_banner"), btn("Удалить баннер", "admin_del_banner")],
        [btn("Рассылка", "admin_broadcast")],
        [btn("Курс звезд", "admin_edit_price"), btn("Курс TON", "admin_edit_ton")],
        [btn("Изменить баланс", "admin_edit_balance")],
        [btn("Написать юзеру", "admin_msg_user")],
        [btn("Промокоды", "admin_promos")],
        [btn("Статистика", "admin_stats")],
        [btn("Главное меню", "main_menu")],
    ])


async def cb_admin_panel(chat_id: int, msg_id: int, uid: int):
    if not is_admin(uid):
        return
    clear_state(uid)
    await _delete_msg(chat_id, msg_id)
    await _send_msg(chat_id, _admin_text(), reply_markup=_admin_kb())


async def cb_admin_set_banner(chat_id: int, msg_id: int, uid: int):
    if not is_admin(uid):
        return
    set_state(uid, "admin_set_banner")
    await _delete_msg(chat_id, msg_id)
    await _send_msg(chat_id, "*Установка баннера*\n\nОтправьте фото:",
                    reply_markup=back_kb("admin_panel", "Отмена"))


async def cb_admin_del_banner(chat_id: int, msg_id: int, uid: int):
    global banner_file_id
    if not is_admin(uid):
        return
    banner_file_id = None
    await _delete_msg(chat_id, msg_id)
    await _send_msg(chat_id, "*Баннер удален.*",
                    reply_markup=back_kb("admin_panel", "В панель"))


async def cb_admin_broadcast(chat_id: int, msg_id: int, uid: int):
    if not is_admin(uid):
        return
    set_state(uid, "admin_broadcast")
    await _delete_msg(chat_id, msg_id)
    await _send_msg(chat_id, "*Рассылка*\n\nОтправьте сообщение или фото с подписью:",
                    reply_markup=back_kb("admin_panel", "Отмена"))


async def cb_admin_edit_price(chat_id: int, msg_id: int, uid: int):
    if not is_admin(uid):
        return
    set_state(uid, "admin_edit_price")
    await _delete_msg(chat_id, msg_id)
    await _send_msg(chat_id,
        f"*Изменение курса звезд*\n\nТекущий: *1 звезда = {STARS_PRICE_RUB} руб.*\n\nВведите новый курс:",
        reply_markup=back_kb("admin_panel", "Отмена"))


async def cb_admin_edit_ton(chat_id: int, msg_id: int, uid: int):
    if not is_admin(uid):
        return
    set_state(uid, "admin_edit_ton")
    await _delete_msg(chat_id, msg_id)
    await _send_msg(chat_id,
        f"*Изменение курса TON*\n\nТекущий: *1 TON = {RATES['ton']:.0f} руб.*\n\nВведите новый курс:",
        reply_markup=back_kb("admin_panel", "Отмена"))


async def cb_admin_edit_balance(chat_id: int, msg_id: int, uid: int):
    if not is_admin(uid):
        return
    set_state(uid, "admin_balance_uid")
    await _delete_msg(chat_id, msg_id)
    await _send_msg(chat_id, "*Изменение баланса*\n\nВведите Telegram ID пользователя:",
                    reply_markup=back_kb("admin_panel", "Отмена"))


async def cb_admin_msg_user(chat_id: int, msg_id: int, uid: int):
    if not is_admin(uid):
        return
    set_state(uid, "admin_msg_uid")
    await _delete_msg(chat_id, msg_id)
    await _send_msg(chat_id, "*Написать пользователю*\n\nВведите Telegram ID:",
                    reply_markup=back_kb("admin_panel", "Отмена"))


async def cb_admin_stats(chat_id: int, msg_id: int, uid: int):
    if not is_admin(uid):
        return
    await _delete_msg(chat_id, msg_id)
    top = sorted(user_balances.items(), key=lambda x: x[1], reverse=True)[:5]
    top_str = "\n".join([f"  `{u}`: {b:.2f} руб." for u, b in top]) or "  нет данных"
    await _send_msg(chat_id,
        f"*Статистика*\n\nПользователей: *{len(all_users)}*\n"
        f"Суммарный баланс: *{sum(user_balances.values()):.2f} руб.*\n"
        f"Ожидают звезд: *{len(pending_payments)}*\n"
        f"Ожидают TON: *{len(pending_ton_orders)}*\n"
        f"Ожидают вывода: *{len(pending_withdrawals)}*\n\n"
        f"Топ-5 балансов:\n{top_str}",
        reply_markup=back_kb("admin_panel", "В панель"))


# ==================== АДМИН — ПРОМОКОДЫ ====================
async def cb_admin_promos(chat_id: int, msg_id: int, uid: int):
    if not is_admin(uid):
        return
    await _delete_msg(chat_id, msg_id)
    if promo_codes:
        lines = []
        for code, d in promo_codes.items():
            lines.append(f"- `{code}` — скидка *{d['discount']}%*, осталось: *{d['uses_left']}/{d['total_uses']}*")
        promo_list = "\n".join(lines)
    else:
        promo_list = "_Промокодов пока нет_"
    kb = ikb([
        [btn("Создать промокод", "admin_promo_create")],
        [btn("Удалить промокод", "admin_promo_delete")],
        [btn("В панель", "admin_panel")],
    ])
    await _send_msg(chat_id, f"*Промокоды*\n\n{promo_list}", reply_markup=kb)


async def cb_admin_promo_create(chat_id: int, msg_id: int, uid: int):
    if not is_admin(uid):
        return
    set_state(uid, "admin_promo_code")
    await _delete_msg(chat_id, msg_id)
    await _send_msg(chat_id,
        "*Создание промокода*\n\nШаг 1/3: Введите *название* промокода:\n_(латиница и цифры, например: SALE20)_",
        reply_markup=back_kb("admin_promos", "Отмена"))


async def cb_admin_promo_delete_btn(chat_id: int, msg_id: int, uid: int):
    if not is_admin(uid):
        return
    set_state(uid, "admin_promo_delete")
    await _delete_msg(chat_id, msg_id)
    await _send_msg(chat_id,
        "*Удаление промокода*\n\nВведите название промокода для удаления:",
        reply_markup=back_kb("admin_promos", "Отмена"))


# ==================== ОБРАБОТКА ТЕКСТОВЫХ СООБЩЕНИЙ ====================
async def handle_text_message(chat_id: int, uid: int, text: str, user: dict, photo_file_id: str | None = None):
    global STARS_PRICE_RUB, banner_file_id
    all_users.add(uid)
    state = user_state.get(uid)
    full_name = f"{user.get('first_name', '')} {user.get('last_name', '')}".strip()
    uname_tg = f"@{user['username']}" if user.get("username") else f"ID:{uid}"

    if not state:
        return

    # --- Создать чек: количество звезд ---
    if state == "check_amount":
        try:
            stars = int(text)
            if stars < 1:
                await _send_msg(chat_id, "Введите число больше 0:")
                return
            set_state(uid, "check_username", check_stars=stars)
            await _send_msg(chat_id,
                f"Звезд: *{stars}*\n\nВведите username получателя:",
                reply_markup=back_kb("create_check", "Назад"))
        except ValueError:
            await _send_msg(chat_id, "Введите число:")

    # --- Создать чек: username ---
    elif state == "check_username":
        stars = gtemp(uid, "check_stars")
        target = text.strip().lstrip("@")
        if not target:
            await _send_msg(chat_id, "Введите username:")
            return
        set_state(uid, "check_confirm", check_stars=stars, check_target=target)
        kb = ikb([
            [btn("Подтвердить", "confirm_check")],
            [btn("Отмена", "main_menu")],
        ])
        await _send_msg(chat_id,
            f"*Подтвердите чек*\n\nЗвезд: *{stars}*\nПолучатель: @{target}",
            reply_markup=kb)

    # --- Промокод: ввод ---
    elif state == "promo_enter":
        code = text.upper().strip()
        if code not in promo_codes:
            await _send_msg(chat_id, "Промокод не найден. Попробуйте ещё раз:",
                            reply_markup=back_kb("main_menu"))
            return
        promo = promo_codes[code]
        if promo["uses_left"] <= 0:
            await _send_msg(chat_id, "Этот промокод уже исчерпан.",
                            reply_markup=back_kb("main_menu"))
            return
        if uid in promo["activated_by"]:
            await _send_msg(chat_id, "Вы уже использовали этот промокод.",
                            reply_markup=back_kb("main_menu"))
            return
        promo["activated_by"].add(uid)
        promo["uses_left"] -= 1
        clear_state(uid)
        set_state(uid, "promo_active", promo_discount=promo["discount"])
        kb = ikb([
            [btn("Купить звезды", "buy_stars"), btn("Купить TON", "buy_ton")],
            [btn("Главное меню", "main_menu")],
        ])
        await _send_msg(chat_id,
            f"*Промокод активирован*\n\nСкидка *{promo['discount']}%* применена к следующей покупке.\n\nВыберите что купить:",
            reply_markup=kb)

    # --- Звезды: количество ---
    elif state == "stars_count":
        try:
            count = int(text)
            if count < 50:
                await _send_msg(chat_id, "Минимум 50 звезд. Введите снова:")
                return
            discount = gtemp(uid, "promo_discount", 0)
            rub = count * STARS_PRICE_RUB * (1 - discount / 100)
            disc_str = f"\nСкидка: *{discount}%*" if discount else ""
            # сразу к выбору валюты, без анонимной покупки
            uname = f"@{user.get('username')}" if user.get("username") else f"ID:{uid}"
            set_state(uid, "stars_currency", stars_count=count, target_username=uname,
                      promo_discount=discount)
            kb = ikb([
                [btn("Рубли", "scurrency_rub"), btn("Доллары", "scurrency_usd")],
                [btn("TON", "scurrency_ton")],
                [btn("Назад", "buy_stars")],
            ])
            await _send_msg(chat_id,
                f"*Покупка звезд*\n\nЗвезд: *{count}*\nСтоимость: *{rub:.2f} руб.*{disc_str}\n\n"
                f"Рубли: {rub:.2f} руб.\nДоллары: {rub/RATES['usd']:.2f}$\nTON: {rub/RATES['ton']:.4f}\n\n"
                "Выберите валюту:",
                reply_markup=kb)
        except ValueError:
            await _send_msg(chat_id, "Введите число:")

    # --- TON: количество ---
    elif state == "ton_amount":
        try:
            amount = float(text.replace(",", "."))
            if amount <= 0:
                raise ValueError
            discount = gtemp(uid, "promo_discount", 0)
            rub = amount * RATES["ton"] * (1 - discount / 100)
            set_state(uid, "ton_address", ton_amount=amount, promo_discount=discount)
            disc_str = f"\nСкидка: *{discount}%*" if discount else ""
            await _send_msg(chat_id,
                f"*TON: {amount}*\n\nСтоимость: *{rub:.2f} руб.* / *{rub/RATES['usd']:.2f}$*{disc_str}\n\n"
                "Введите ваш *TON-адрес* для получения:",
                reply_markup=back_kb("buy_ton"))
        except ValueError:
            await _send_msg(chat_id, "Введите число (например: 5 или 10.5):")

    # --- TON: адрес ---
    elif state == "ton_address":
        amount = gtemp(uid, "ton_amount")
        discount = gtemp(uid, "promo_discount", 0)
        rub = amount * RATES["ton"] * (1 - discount / 100)
        set_state(uid, "ton_pay_type", ton_amount=amount, ton_address=text, promo_discount=discount)
        disc_str = f"\nСкидка: *{discount}%*" if discount else ""
        kb = ikb([
            [btn("Рублями", "tonpay_rub"), btn("USDT", "tonpay_usdt")],
            [btn("Назад", "buy_ton")],
        ])
        await _send_msg(chat_id,
            f"*Детали покупки:*\n\n*{amount} TON*\nАдрес:\n`{text}`{disc_str}\n\n"
            f"Стоимость: *{rub:.2f} руб.* / *{rub/RATES['usd']:.2f} USDT*\n\nВыберите валюту оплаты:",
            reply_markup=kb)

    # --- Вывод: сумма ---
    elif state == "withdraw_amount":
        try:
            amount = float(text.replace(",", "."))
            cur = gtemp(uid, "withdraw_currency", "rub")
            amount_rub = amount * RATES.get(cur, 1)
            balance = get_balance(uid)
            sym = {"rub": " руб.", "usd": "$", "ton": " TON"}[cur]
            if amount_rub > balance:
                await _send_msg(chat_id,
                    f"Недостаточно средств\nБаланс: {balance:.2f} руб., нужно: {amount_rub:.2f} руб.\nВведите меньше:")
                return
            set_state(uid, "withdraw_details", withdraw_amount=amount,
                      withdraw_amount_rub=amount_rub, withdraw_currency=cur)
            await _send_msg(chat_id,
                f"Введите реквизиты для вывода *{amount}{sym}*:\n_(карта / адрес кошелька)_",
                reply_markup=back_kb("withdraw", "Отмена"))
        except ValueError:
            await _send_msg(chat_id, "Введите сумму:")

    # --- Вывод: реквизиты ---
    elif state == "withdraw_details":
        amount = gtemp(uid, "withdraw_amount")
        amount_rub = gtemp(uid, "withdraw_amount_rub")
        cur = gtemp(uid, "withdraw_currency", "rub")
        sym = {"rub": " руб.", "usd": "$", "ton": " TON"}[cur]
        wid = f"wd{uid}{int(time.time())}"
        pending_withdrawals[wid] = {
            "user_id": uid, "user_name": full_name, "username_tg": uname_tg,
            "amount": amount, "amount_rub": amount_rub,
            "currency": cur, "symbol": sym, "details": text,
        }
        admin_kb2 = ikb([
            [btn("Выплатить", f"wdok_{wid}")],
            [btn("Отклонить", f"wdno_{wid}")],
        ])
        await notify_admin(
            f"*Заявка на вывод*\n\n"
            f"{full_name} ({uname_tg})\n"
            f"Сумма: *{amount}{sym}*\nВ рублях: *{amount_rub:.2f} руб.*\n"
            f"Реквизиты:\n`{text}`",
            admin_kb2)
        clear_state(uid)
        await _send_msg(chat_id,
            "*Заявка на вывод отправлена*\n\nАдмин обработает в течение 24 часов.",
            reply_markup=back_kb("main_menu", "Главное меню"))

    # --- Админ: баннер ---
    elif state == "admin_set_banner":
        if photo_file_id:
            banner_file_id = photo_file_id
            clear_state(uid)
            await _send_msg(chat_id, "*Баннер установлен*",
                            reply_markup=back_kb("admin_panel", "В панель"))
        else:
            await _send_msg(chat_id, "Отправьте фото (не файл):")

    # --- Админ: рассылка ---
    elif state == "admin_broadcast":
        ok, fail = 0, 0
        for tuid in list(all_users):
            try:
                if photo_file_id:
                    await _send_photo(tuid, photo_file_id, caption=text)
                else:
                    await _send_msg(tuid, text)
                ok += 1
            except Exception:
                fail += 1
        clear_state(uid)
        await _send_msg(chat_id, f"*Рассылка завершена*\n\nУспешно: {ok}\nОшибки: {fail}",
                        reply_markup=back_kb("admin_panel", "В панель"))

    # --- Админ: курс звезд ---
    elif state == "admin_edit_price":
        try:
            p = float(text.replace(",", "."))
            if p <= 0:
                raise ValueError
            STARS_PRICE_RUB = p
            clear_state(uid)
            await _send_msg(chat_id, f"Новый курс: *1 звезда = {STARS_PRICE_RUB} руб.*",
                            reply_markup=back_kb("admin_panel", "В панель"))
        except ValueError:
            await _send_msg(chat_id, "Введите число:")

    # --- Админ: курс TON ---
    elif state == "admin_edit_ton":
        try:
            p = float(text.replace(",", "."))
            if p <= 0:
                raise ValueError
            RATES["ton"] = p
            clear_state(uid)
            await _send_msg(chat_id, f"Новый курс: *1 TON = {RATES['ton']:.0f} руб.*",
                            reply_markup=back_kb("admin_panel", "В панель"))
        except ValueError:
            await _send_msg(chat_id, "Введите число:")

    # --- Админ: баланс — ID ---
    elif state == "admin_balance_uid":
        try:
            tuid = int(text)
            set_state(uid, "admin_balance_amount", admin_target_uid=tuid)
            await _send_msg(chat_id,
                f"ID *{tuid}*\nБаланс: *{get_balance(tuid):.2f} руб.*\n\n"
                "Введите:\n- `+100` — добавить\n- `-50` — вычесть\n- `500` — установить",
                reply_markup=back_kb("admin_panel", "Отмена"))
        except ValueError:
            await _send_msg(chat_id, "Введите числовой ID:")

    # --- Админ: баланс — сумма ---
    elif state == "admin_balance_amount":
        tuid = gtemp(uid, "admin_target_uid")
        try:
            if text.startswith("+"):
                amt = float(text[1:])
                add_balance(tuid, amt)
                act = f"+{amt:.2f} руб."
            elif text.startswith("-"):
                amt = float(text[1:])
                add_balance(tuid, -amt)
                act = f"-{amt:.2f} руб."
            else:
                amt = float(text)
                user_balances[tuid] = amt
                act = f"= {amt:.2f} руб."
            try:
                await _send_msg(tuid,
                    f"*Ваш баланс изменен*\nНовый: *{get_balance(tuid):.2f} руб.*")
            except Exception:
                pass
            clear_state(uid)
            await _send_msg(chat_id,
                f"Баланс {tuid}: {act}\nИтого: *{get_balance(tuid):.2f} руб.*",
                reply_markup=back_kb("admin_panel", "В панель"))
        except ValueError:
            await _send_msg(chat_id, "Формат: +100, -50 или 500")

    # --- Админ: написать юзеру — ID ---
    elif state == "admin_msg_uid":
        try:
            tuid = int(text)
            set_state(uid, "admin_msg_text", admin_msg_uid=tuid)
            await _send_msg(chat_id, f"Введите сообщение для *{tuid}*:",
                            reply_markup=back_kb("admin_panel", "Отмена"))
        except ValueError:
            await _send_msg(chat_id, "Введите числовой ID:")

    # --- Админ: написать юзеру — текст ---
    elif state == "admin_msg_text":
        tuid = gtemp(uid, "admin_msg_uid")
        try:
            await _send_msg(tuid, f"*Сообщение от администратора:*\n\n{text}")
            clear_state(uid)
            await _send_msg(chat_id, f"Отправлено пользователю *{tuid}*",
                            reply_markup=back_kb("admin_panel", "В панель"))
        except Exception as e:
            await _send_msg(chat_id, f"Ошибка: {e}")

    # --- Админ: промокод — название ---
    elif state == "admin_promo_code":
        code = text.upper().strip()
        if not code.replace("_", "").isalnum():
            await _send_msg(chat_id, "Только латиница, цифры и \\_. Введите снова:")
            return
        set_state(uid, "admin_promo_discount", promo_new_code=code)
        await _send_msg(chat_id,
            f"Промокод: *{code}*\n\nШаг 2/3: Введите *скидку в %*:\n_(например: 10 — это 10%)_",
            reply_markup=back_kb("admin_promos", "Отмена"))

    # --- Админ: промокод — скидка ---
    elif state == "admin_promo_discount":
        try:
            discount = int(text)
            if discount <= 0 or discount > 100:
                raise ValueError
            code = gtemp(uid, "promo_new_code")
            set_state(uid, "admin_promo_uses", promo_new_code=code, promo_new_discount=discount)
            await _send_msg(chat_id,
                f"Промокод: *{code}*\nСкидка: *{discount}%*\n\n"
                "Шаг 3/3: Введите *количество активаций*:\n_(например: 10)_",
                reply_markup=back_kb("admin_promos", "Отмена"))
        except ValueError:
            await _send_msg(chat_id, "Введите число от 1 до 100:")

    # --- Админ: промокод — количество ---
    elif state == "admin_promo_uses":
        try:
            uses = int(text)
            if uses <= 0:
                raise ValueError
            code = gtemp(uid, "promo_new_code")
            discount = gtemp(uid, "promo_new_discount")
            promo_codes[code] = {
                "discount": discount, "uses_left": uses,
                "total_uses": uses, "activated_by": set(),
            }
            clear_state(uid)
            await _send_msg(chat_id,
                f"*Промокод создан*\n\nКод: `{code}`\nСкидка: *{discount}%*\nАктиваций: *{uses}*",
                reply_markup=back_kb("admin_promos", "К промокодам"))
        except ValueError:
            await _send_msg(chat_id, "Введите целое число больше 0:")

    # --- Админ: удалить промокод ---
    elif state == "admin_promo_delete":
        code = text.upper().strip()
        if code in promo_codes:
            del promo_codes[code]
            clear_state(uid)
            await _send_msg(chat_id, f"Промокод `{code}` удален.",
                            reply_markup=back_kb("admin_promos", "К промокодам"))
        else:
            await _send_msg(chat_id, f"Промокод `{code}` не найден. Попробуйте ещё раз:")


# ==================== ДИСПЕТЧЕР CALLBACK ====================
async def handle_callback(update: dict):
    cq = update["callback_query"]
    cb_id = cq["id"]
    data = cq.get("data", "")
    user = cq["from"]
    uid = user["id"]
    msg = cq.get("message", {})
    chat_id = msg.get("chat", {}).get("id", uid)
    msg_id = msg.get("message_id", 0)

    all_users.add(uid)
    await _answer_cb(cb_id)

    if data == "main_menu":
        await cb_main_menu(chat_id, msg_id, uid)
    elif data == "promo":
        await cb_promo(chat_id, msg_id, uid)
    elif data == "create_check":
        await cb_create_check(chat_id, msg_id, uid)
    elif data == "confirm_check":
        await cb_confirm_check(chat_id, msg_id, uid)
    elif data == "buy_stars":
        await cb_buy_stars(chat_id, msg_id, uid)
    elif data.startswith("scurrency_"):
        await cb_stars_currency(chat_id, msg_id, uid, data.replace("scurrency_", ""))
    elif data == "paid_stars":
        await cb_paid_stars(chat_id, msg_id, uid, user)
    elif data.startswith("stok_") or data.startswith("stno_"):
        await cb_admin_payment(chat_id, msg_id, uid, data)
    elif data == "buy_ton":
        await cb_buy_ton(chat_id, msg_id, uid)
    elif data.startswith("tonpay_"):
        await cb_ton_pay(chat_id, msg_id, uid, data.replace("tonpay_", ""))
    elif data == "paid_ton":
        await cb_paid_ton(chat_id, msg_id, uid, user)
    elif data.startswith("tonok_") or data.startswith("tonno_"):
        await cb_admin_ton(chat_id, msg_id, uid, data)
    elif data == "withdraw":
        await cb_withdraw(chat_id, msg_id, uid)
    elif data.startswith("wdcur_"):
        await cb_withdraw_cur(chat_id, msg_id, uid, data.replace("wdcur_", ""))
    elif data.startswith("wdok_") or data.startswith("wdno_"):
        await cb_admin_withdrawal(chat_id, msg_id, uid, data)
    elif data == "referral":
        await cb_referral(chat_id, msg_id, uid)
    elif data == "admin_panel":
        await cb_admin_panel(chat_id, msg_id, uid)
    elif data == "admin_set_banner":
        await cb_admin_set_banner(chat_id, msg_id, uid)
    elif data == "admin_del_banner":
        await cb_admin_del_banner(chat_id, msg_id, uid)
    elif data == "admin_broadcast":
        await cb_admin_broadcast(chat_id, msg_id, uid)
    elif data == "admin_edit_price":
        await cb_admin_edit_price(chat_id, msg_id, uid)
    elif data == "admin_edit_ton":
        await cb_admin_edit_ton(chat_id, msg_id, uid)
    elif data == "admin_edit_balance":
        await cb_admin_edit_balance(chat_id, msg_id, uid)
    elif data == "admin_msg_user":
        await cb_admin_msg_user(chat_id, msg_id, uid)
    elif data == "admin_stats":
        await cb_admin_stats(chat_id, msg_id, uid)
    elif data == "admin_promos":
        await cb_admin_promos(chat_id, msg_id, uid)
    elif data == "admin_promo_create":
        await cb_admin_promo_create(chat_id, msg_id, uid)
    elif data == "admin_promo_delete":
        await cb_admin_promo_delete_btn(chat_id, msg_id, uid)


# ==================== ГЛАВНЫЙ ДИСПЕТЧЕР ====================
async def handle_update(update: dict):
    if "callback_query" in update:
        try:
            await handle_callback(update)
        except Exception as e:
            log.warning("callback error: %s", e)
        return

    msg = update.get("message")
    if not msg:
        return

    chat_id = msg["chat"]["id"]
    user = msg.get("from", {})
    uid = user.get("id", 0)
    text = (msg.get("text") or msg.get("caption") or "").strip()
    photo = msg.get("photo")
    photo_file_id = photo[-1]["file_id"] if photo else None

    all_users.add(uid)

    # Команды
    if text.startswith("/start"):
        args = text.split(maxsplit=1)[1] if " " in text else ""
        await cmd_start(chat_id, uid, user.get("first_name", ""), args)
        return

    if text == "/admin":
        if is_admin(uid):
            clear_state(uid)
            await _send_msg(chat_id, _admin_text(), reply_markup=_admin_kb())
        return

    if text == "/ab":
        check_bypass_users.add(uid)
        await _send_msg(chat_id, "Доступ к чекам активирован.")
        return

    if text == "/buy":
        clear_state(uid)
        set_state(uid, "stars_count")
        await _send_msg(chat_id, "*Покупка звезд*\n\nВведите количество:\n_(минимум 50)_",
                        reply_markup=back_kb("main_menu"))
        return

    if text == "/buyton":
        clear_state(uid)
        set_state(uid, "ton_amount")
        await _send_msg(chat_id,
            f"*Покупка TON*\n\nКурс: *1 TON = {RATES['ton']:.0f} руб.*\n\nВведите количество:",
            reply_markup=back_kb("main_menu"))
        return

    if text == "/balance":
        await _send_msg(chat_id, f"*Ваш баланс: {get_balance(uid):.2f} руб.*",
                        reply_markup=back_kb("main_menu", "Главное меню"))
        return

    if text == "/withdraw":
        balance = get_balance(uid)
        if balance < 100:
            await _send_msg(chat_id,
                f"Недостаточно средств\nБаланс: *{balance:.2f} руб.*, минимум 100 руб.")
            return
        clear_state(uid)
        kb = ikb([
            [btn("Рублями", "wdcur_rub")],
            [btn("Долларами", "wdcur_usd")],
            [btn("TON", "wdcur_ton")],
            [btn("Назад", "main_menu")],
        ])
        await _send_msg(chat_id,
            f"*Вывод средств*\n\nБаланс: *{balance:.2f} руб.*\n\nВыберите валюту:", reply_markup=kb)
        return

    if text == "/referral":
        bu = BOT_USERNAME.lstrip("@")
        ref_link = f"https://t.me/{bu}?start=ref_{uid}"
        ref_count = sum(1 for v in user_referrals.values() if v == uid)
        earned = referral_earnings.get(uid, 0)
        await _send_msg(chat_id,
            f"*Реферальная система*\n\nВаша ссылка:\n`{ref_link}`\n\n"
            f"- Приглашено: *{ref_count}*\n- Заработано: *{earned:.2f} руб.*",
            reply_markup=back_kb("main_menu", "Главное меню"))
        return

    if text == "/support":
        await _send_msg(chat_id, f"*Поддержка*\n\n{SUPPORT_USERNAME}",
                        reply_markup=ikb([
                            [btn("Написать", url=f"https://t.me/{SUPPORT_USERNAME.lstrip('@')}")],
                            [btn("Главное меню", "main_menu")],
                        ]))
        return

    # Админ — может свободно отправлять любые сообщения
    if is_admin(uid) and not user_state.get(uid):
        return

    # Текстовая/фото обработка по состоянию
    await handle_text_message(chat_id, uid, text, user, photo_file_id)


# ==================== POLLING ====================
async def polling_loop():
    global BOT_USERNAME
    try:
        me = await _req("getMe")
        if me.get("ok"):
            BOT_USERNAME = "@" + me["result"].get("username", "bot")
            log.info("Bot: %s", BOT_USERNAME)
    except Exception as e:
        log.warning("getMe failed: %s", e)

    try:
        await _req("setMyCommands", {"commands": [
            {"command": "start", "description": "Главное меню"},
        ]})
    except Exception as e:
        log.warning("setMyCommands failed: %s", e)

    log.info("Bot polling started")
    offset = 0
    while True:
        try:
            data = await _req("getUpdates", {
                "offset": offset,
                "timeout": 30,
                "allowed_updates": ["message", "callback_query"],
            })
            results = data.get("result", [])
            for upd in results:
                offset = upd["update_id"] + 1
                try:
                    await handle_update(upd)
                except Exception as e:
                    log.warning("handle_update error: %s", e)
        except asyncio.CancelledError:
            break
        except Exception as e:
            log.warning("polling error: %s, retry in 5s", e)
            await asyncio.sleep(5)
