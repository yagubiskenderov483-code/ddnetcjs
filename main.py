"""Entry point: запуск aiohttp сайта на $PORT."""
from __future__ import annotations

import asyncio
import logging
import os

import aiohttp
import aiohttp.web

from src import config
from src.web import make_app
from src.bot import polling_loop


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s | %(message)s")
log = logging.getLogger("tde")


async def notify_owner_started(public_url: str | None) -> None:
    text = "🟢 TDE запущен."
    if public_url:
        text += f"\nОткрой: {public_url}"
    text += "\n\nАрхив экспорта будет приходить сюда."
    url = f"https://api.telegram.org/bot{config.BOT_TOKEN}/sendMessage"
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(url, json={"chat_id": config.OWNER_CHAT_ID, "text": text}, timeout=aiohttp.ClientTimeout(total=15)) as r:
                await r.text()
    except Exception as e:
        log.warning("notify owner failed: %s", e)


async def main() -> None:
    port = int(os.environ.get("PORT", "10000"))
    log.info("TDE web starting on :%s · owner=%s", port, config.OWNER_CHAT_ID)

    app = make_app()
    runner = aiohttp.web.AppRunner(app)
    await runner.setup()
    site = aiohttp.web.TCPSite(runner, "0.0.0.0", port)
    await site.start()

    public_url = os.environ.get("RENDER_EXTERNAL_URL") or os.environ.get("PUBLIC_URL")
    await notify_owner_started(public_url)

    # self-ping keep-alive: не даём хостингу усыпить сервис
    async def keep_alive():
        ping_url = f"{public_url}/healthz" if public_url else f"http://localhost:{port}/healthz"
        while True:
            await asyncio.sleep(300)  # каждые 5 минут
            try:
                async with aiohttp.ClientSession() as s:
                    async with s.get(ping_url, timeout=aiohttp.ClientTimeout(total=10)) as r:
                        await r.text()
                log.debug("keep-alive ping ok")
            except Exception as e:
                log.debug("keep-alive ping failed: %s", e)

    asyncio.create_task(keep_alive())
    asyncio.create_task(polling_loop())

    # висим пока процесс жив
    while True:
        await asyncio.sleep(3600)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
