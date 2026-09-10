"""Отправка сообщений в Telegram напрямую через Bot API (агент сам писать первым не умеет)."""
import asyncio
import logging

import httpx

from . import config

log = logging.getLogger("byt.telegram")

API = "https://api.telegram.org"


async def send(text: str, chat_id: str | None = None) -> bool:
    chat_id = chat_id or config.TELEGRAM_CHAT_ID
    if not config.TELEGRAM_BOT_TOKEN or not chat_id:
        log.warning("Telegram не настроен: нет TELEGRAM_BOT_TOKEN или TELEGRAM_CHAT_ID")
        return False
    url = f"{API}/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage"
    last_error = None
    for attempt in range(1, 4):
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(30, connect=15)) as client:
                r = await client.post(url, json={"chat_id": chat_id, "text": text})
            if r.status_code == 200:
                return True
            log.error("Telegram ответил %s: %s", r.status_code, r.text[:300])
            return False
        except Exception as e:  # noqa: BLE001
            last_error = e
            log.warning("Telegram, попытка %d не удалась: %s: %s", attempt, type(e).__name__, e or "(без текста)")
            await asyncio.sleep(5 * attempt)
    log.error("Telegram недоступен после 3 попыток: %s: %s", type(last_error).__name__, last_error)
    return False


async def diagnose() -> str:
    """Что видно из контейнера: DNS, TCP, ответ getMe. Для страницы /diag."""
    import socket
    out = []
    try:
        out.append(f"dns api.telegram.org -> {socket.gethostbyname('api.telegram.org')}")
    except Exception as e:  # noqa: BLE001
        out.append(f"dns fail: {type(e).__name__}: {e}")
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(20, connect=10)) as client:
            r = await client.get(f"{API}/bot{config.TELEGRAM_BOT_TOKEN}/getMe")
        out.append(f"getMe -> {r.status_code} {r.text[:120]}")
    except Exception as e:  # noqa: BLE001
        out.append(f"getMe fail: {type(e).__name__}: {e}")
    return "\n".join(out)
