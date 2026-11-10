# Публикация на VPS

Минимальный production-стек: VPS (Ubuntu 22.04+), домен, Caddy как reverse-proxy с автоматическим HTTPS.

## 1. Подготовка сервера

```bash
sudo apt update && sudo apt install -y docker.io docker-compose-plugin ufw
sudo ufw allow 22,80,443/tcp && sudo ufw enable
```

## 2. Клонирование и .env

```bash
git clone <repo> /opt/tde && cd /opt/tde
cp config/.env.example .env
# заполнить TG_API_ID, TG_API_HASH, BOT_TOKEN, OWNER_CHAT_ID,
# SESSION_SECRET=$(openssl rand -hex 32)
# FRONTEND_ORIGIN=https://tde.yourdomain.com
# NEXT_PUBLIC_API_BASE=https://api.tde.yourdomain.com
```

## 3. DNS

Две A-записи на IP сервера:

```
tde.yourdomain.com         → <IP>
api.tde.yourdomain.com     → <IP>
```

## 4. Caddyfile

`/etc/caddy/Caddyfile`:

```caddyfile
tde.yourdomain.com {
    reverse_proxy 127.0.0.1:3000
    encode zstd gzip
}

api.tde.yourdomain.com {
    reverse_proxy 127.0.0.1:8000
    encode zstd gzip
    header {
        Strict-Transport-Security "max-age=31536000"
        X-Content-Type-Options "nosniff"
        Referrer-Policy "no-referrer"
    }
}
```

```bash
sudo apt install -y caddy
sudo systemctl enable --now caddy
```

Caddy выпустит сертификаты Let's Encrypt автоматически.

## 5. Запуск сервисов

```bash
cd /opt/tde
docker compose up -d --build
docker compose logs -f
```

## 6. Автозапуск после ребута

docker compose с `restart: unless-stopped` уже настроен — сервисы поднимутся сами.

## 7. Проверка

* `https://tde.yourdomain.com` — фронт.
* `https://api.tde.yourdomain.com/healthz` — `{"ok":true}`.
* `https://api.tde.yourdomain.com/docs` — Swagger.

## 8. Обновление

```bash
cd /opt/tde && git pull && docker compose up -d --build
```

## 9. Чеклист безопасности

- [ ] `SESSION_SECRET` сгенерирован случайно, длиной ≥ 32 байт
- [ ] `.env` не закоммичен (`.gitignore` уже содержит)
- [ ] `FRONTEND_ORIGIN` строгий (один домен, не `*`)
- [ ] 80/443 открыты, остальное закрыто ufw
- [ ] Логи не содержат номер телефона и код (проверь `docker compose logs backend | grep +`)
- [ ] Автоудаление временных файлов работает (`TEMP_TTL_SECONDS` и финальный `cleanup_archive`)
- [ ] В Caddyfile стоит HSTS

## 10. Альтернативы Caddy

Если нужен Nginx — конфиг аналогичный, с `certbot`. Caddy проще для одного сервиса.
