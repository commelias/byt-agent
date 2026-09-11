"""Отправка сообщений в Telegram напрямую через Bot API (агент сам писать первым не умеет)."""
import asyncio
import logging

import httpx

from . import config

log = logging.getLogger("byt.telegram")

API = "https://api.telegram.org"
ATTEMPTS = 4


def _client() -> httpx.AsyncClient:
    """Только IPv4 (local_address 0.0.0.0): из дата-центра путь к Telegram по IPv6 бывает
    недоступен, и соединение висит до таймаута. Короткий таймаут соединения + несколько попыток
    переживают частые «моргания» связи с api.telegram.org."""
    transport = httpx.AsyncHTTPTransport(local_address="0.0.0.0", retries=1)
    return httpx.AsyncClient(transport=transport, timeout=httpx.Timeout(30, connect=8))


async def send(text: str, chat_id: str | None = None) -> bool:
    chat_id = chat_id or config.TELEGRAM_CHAT_ID
    if not config.TELEGRAM_BOT_TOKEN or not chat_id:
        log.warning("Telegram не настроен: нет TELEGRAM_BOT_TOKEN или TELEGRAM_CHAT_ID")
        return False
    url = f"{API}/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage"
    last_error = None
    for attempt in range(1, ATTEMPTS + 1):
        try:
            async with _client() as client:
                r = await client.post(url, json={"chat_id": chat_id, "text": text})
            if r.status_code == 200:
                return True
            log.error("Telegram ответил %s: %s", r.status_code, r.text[:300])
            return False
        except Exception as e:  # noqa: BLE001
            last_error = e
            log.warning("Telegram, попытка %d не удалась: %s: %s", attempt, type(e).__name__, e or "(без текста)")
            await asyncio.sleep(3 * attempt)
    log.error("Telegram недоступен после %d попыток: %s: %s", ATTEMPTS, type(last_error).__name__, last_error)
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
        async with _client() as client:
            r = await client.get(f"{API}/bot{config.TELEGRAM_BOT_TOKEN}/getMe")
        out.append(f"getMe -> {r.status_code} {r.text[:120]}")
    except Exception as e:  # noqa: BLE001
        out.append(f"getMe fail: {type(e).__name__}: {e}")
    return "\n".join(out)
