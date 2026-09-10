"""Отправка сообщений в Telegram напрямую через Bot API (агент сам писать первым не умеет)."""
import logging

import httpx

from . import config

log = logging.getLogger("byt.telegram")


async def send(text: str, chat_id: str | None = None) -> bool:
    chat_id = chat_id or config.TELEGRAM_CHAT_ID
    if not config.TELEGRAM_BOT_TOKEN or not chat_id:
        log.warning("Telegram не настроен: нет TELEGRAM_BOT_TOKEN или TELEGRAM_CHAT_ID")
        return False
    url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage"
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.post(url, json={"chat_id": chat_id, "text": text})
    if r.status_code != 200:
        log.error("Telegram ответил %s: %s", r.status_code, r.text[:300])
        return False
    return True
