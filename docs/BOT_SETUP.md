# Настройка Telegram-бота

## 1. Создать бота

1. В Telegram открыть [@BotFather](https://t.me/BotFather).
2. `/newbot` → задать имя и username (`something_bot`).
3. BotFather вернёт токен вида `123456789:AAH...`. Это `BOT_TOKEN` в `.env`.

**Токен хранится только на сервере.** В браузер никогда не отправляется.

## 2. Получить OWNER_CHAT_ID

Бот должен отправлять архивы владельцу — вам. Нужно узнать ваш `chat_id`.

### Вариант A: через getUpdates

1. Запустить `/start` вашему боту со своего аккаунта.
2. Открыть в браузере:

```
https://api.telegram.org/bot<BOT_TOKEN>/getUpdates
```

3. Найти `"chat":{"id":123456789,…}` — это ваш `chat_id`.

### Вариант B: через @userinfobot

Напишите [@userinfobot](https://t.me/userinfobot) — он вернёт ваш ID одним сообщением.

### Вариант C: через встроенный `/id`

В этом проекте бот поддерживает `/id` — напишите боту, получите ответ.

Запишите в `.env`:

```
BOT_TOKEN=123456789:AAH...
OWNER_CHAT_ID=123456789
```

## 3. Проверить

После запуска бота напишите ему `/start` — должно прийти приветствие. Напишите `/id` — получите chat_id. Если chat_id не совпадает с `OWNER_CHAT_ID`, исправьте `.env` и перезапустите.

## 4. Как бэкенд шлёт архив

Бэкенд делает прямой `POST https://api.telegram.org/bot<TOKEN>/sendDocument` с `chat_id=OWNER_CHAT_ID`. Бот-процесс на `aiogram` нужен только чтобы подтверждать получение и отвечать на команды — само поступление архива работает и без него.

## 5. Лимиты Bot API

* `sendDocument` — до 50 MB. Архив больше режется на `.part01`, `.part02`...
* Склеивание: `cat telegram_export_*.part* > telegram_export.zip`
