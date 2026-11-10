# Установка (без Docker)

Требования: Python 3.11+, Node.js 20+, Linux/macOS/Windows.

## 1. Получение Bot Token

См. [docs/BOT_SETUP.md](BOT_SETUP.md).

## 2. .env

```bash
cp config/.env.example .env
# openssl rand -hex 32   → вставить в SESSION_SECRET
vim .env
```

## 3. Backend

```bash
cd backend
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python main.py
# или:  uvicorn main:app --host 0.0.0.0 --port 8000
```

Проверка: `curl http://localhost:8000/healthz` → `{"ok": true, "version": "2.0.0"}`

## 4. Bot (опционально)

```bash
cd bot
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python main.py
```

## 5. Frontend

```bash
cd frontend
npm install
echo "NEXT_PUBLIC_API_BASE=http://localhost:8000" > .env.local
npm run dev
```

Открыть [http://localhost:3000](http://localhost:3000).

## 6. Агент (на машине пользователя)

```bash
cd agent
pip install -e .
```

См. [docs/AGENT.md](AGENT.md) для полного описания.

## 7. Проверка полного флоу

1. На сайте → «Сгенерировать pairing-код» → получили 6-значный код.
2. В терминале с установленным агентом:

```bash
export TG_API_ID=...
export TG_API_HASH=...
export TDE_API_BASE=http://localhost:8000
tde-agent pair ABCDEF
```

3. Ввести номер и код из Telegram (локально).
4. Агент выгружает данные, пакует ZIP, грузит на бэкенд.
5. На сайте прогресс идёт от 0 до 100.
6. В личке с ботом — приходит ZIP (или части, если >50 MB).
