# Telegram Data Extractor

Веб-сервис для полного экспорта данных Telegram-аккаунта в формате,
совместимом с Telegram Desktop Export (HTML + JSON + медиа по папкам).

## Архитектура

```
┌─────────────┐      HTTPS      ┌──────────────┐
│  Frontend   │  ◄───────────►  │   Backend    │
│  Next.js    │   pairing       │   FastAPI    │
│             │   status        │   (relay)    │
└─────────────┘                 └──────┬───────┘
                                       │ Bot API
                                       ▼
                                ┌──────────────┐
                                │  Owner Bot   │
                                └──────────────┘
      ▲
      │ pairing code
      │
┌─────┴────────┐    MTProto    ┌──────────┐
│ Local Agent  │  ───────────► │ Telegram │
│ (user's PC)  │               │          │
│ Telethon     │               └──────────┘
│ + HTML/JSON  │
│   exporter   │
└──────┬───────┘
       │ chunked upload (ZIP + sha256)
       └─► Backend
```

**Ключевое отличие от v1:** авторизация Telegram и выгрузка данных
выполняются на машине владельца аккаунта, через локальный Python-агент
(`tde-agent`). Серверу уходит только готовый ZIP-архив после сверки
sha256. Коды авторизации и пароли 2FA сервер не видит.

## Компоненты

* **frontend/** — Next.js 14 (App Router) + TypeScript + Tailwind + Framer Motion. Тёмный премиальный UI, генерация pairing-кода, прогресс.
* **backend/** — FastAPI. Выдача pairing-кодов, chunked-приём ZIP от агента, пересылка в Telegram-бот через Bot API (с дроблением на части при >50 MB).
* **agent/** — локальный CLI на Telethon. Полный экспорт: сообщения, фото, видео, голосовые, круглые видеосообщения, документы, стикеры, анимации, контакты. HTML+JSON как у Telegram Desktop.
* **bot/** — приёмный aiogram-бот. Опциональный, реагирует на `/start` и `/id`, подтверждает получение архива.

## Быстрый старт

```bash
git clone <repo> telegram-data-extractor && cd telegram-data-extractor
cp config/.env.example .env
# BOT_TOKEN, OWNER_CHAT_ID, SESSION_SECRET
docker compose up --build
```

На машине владельца Telegram-аккаунта:

```bash
cd agent && pip install -e .
export TG_API_ID=...
export TG_API_HASH=...
export TDE_API_BASE=http://localhost:8000
tde-agent pair <CODE_WITH_SITE>
```

## Что экспортируется

Полная параллель с официальным Telegram Desktop Export:

* Профиль, контакты, список диалогов
* Полная история сообщений (без лимита либо до `--limit`)
* Фото, видео, голосовые, круглые видеосообщения, документы, стикеры, анимации
* JSON для машинной обработки + HTML для чтения
* Структура папок `chats/<id>_<name>/{photos,video_files,voice_messages,round_video_messages,files,stickers,animations}/`

## Что НЕ экспортируется (честно)

* **Секретные (end-to-end) чаты** — по дизайну недоступны ни через MTProto, ни через Telegram Desktop.
* Медиафайлы больше `--max-file-mb` MB пропускаются (по умолчанию 50).

## Безопасность

* Коды авторизации, SMS, пароль 2FA — только локально на машине пользователя.
* Между агентом и бэкендом — HMAC-токен, выданный в ответ на одноразовый 6-значный pairing-код (TTL 15 мин).
* Контрольная сумма ZIP (sha256) сверяется на сервере перед доставкой в бота.
* Токен бота только в `.env`, никогда не попадает в браузер.
* Временные файлы агента удаляются после загрузки. Сервер удаляет ZIP после доставки.
* Rate-limit на эндпоинты выдачи pairing и claim со стороны агента.

## Документация

* [docs/INSTALL.md](docs/INSTALL.md) — установка без Docker
* [docs/AGENT.md](docs/AGENT.md) — установка и запуск локального агента
* [docs/BOT_SETUP.md](docs/BOT_SETUP.md) — создание Telegram-бота
* [docs/DEPLOY.md](docs/DEPLOY.md) — публикация на VPS с HTTPS
