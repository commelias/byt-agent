"""Отправка сообщений в Telegram напрямую через Bot API (агент сам писать первым не умеет)."""
import asyncio
import logging

import httpx

from . import config

log = logging.getLogger("byt.telegram")
# httpx печатает полный адрес запроса, а в нём токен бота — в логах приложения ему не место
logging.getLogger("httpx").setLevel(logging.WARNING)

API = "https://api.telegram.org"
ATTEMPTS = 4


def _client() -> httpx.AsyncClient:
    """Только IPv4 (local_address 0.0.0.0): из дата-центра путь к Telegram по IPv6 бывает
    недоступен, и соединение висит до таймаута. Короткий таймаут соединения + несколько попыток
    переживают частые «моргания» связи с api.telegram.org."""
    transport = httpx.AsyncHTTPTransport(local_address="0.0.0.0", retries=1)
    return httpx.AsyncClient(transport=transport, timeout=httpx.Timeout(30, connect=8))


async def _call(method: str, payload: dict, attempts: int = ATTEMPTS) -> bool:
    """Один вызов Bot API с повторами: связь дата-центра с Telegram периодически моргает."""
    url = f"{API}/bot{config.TELEGRAM_BOT_TOKEN}/{method}"
    last_error = None
    for attempt in range(1, attempts + 1):
        try:
            async with _client() as client:
                r = await client.post(url, json=payload)
            if r.status_code == 200:
                return True
            log.error("Telegram (%s) ответил %s: %s", method, r.status_code, r.text[:300])
            return False
        except Exception as e:  # noqa: BLE001
            last_error = e
            log.warning("Telegram (%s), попытка %d не удалась: %s: %s", method, attempt, type(e).__name__, e or "(без текста)")
            if attempt < attempts:
                await asyncio.sleep(3 * attempt)
    log.error("Telegram недоступен после %d попыток: %s: %s", attempts, type(last_error).__name__, last_error)
    return False


async def send(text: str, chat_id: str | None = None) -> bool:
    chat_id = chat_id or config.TELEGRAM_CHAT_ID
    if not config.TELEGRAM_BOT_TOKEN or not chat_id:
        log.warning("Telegram не настроен: нет TELEGRAM_BOT_TOKEN или TELEGRAM_CHAT_ID")
        return False
    return await _call("sendMessage", {"chat_id": chat_id, "text": text})


async def _upload(method: str, data: dict, files: dict) -> bool:
    """Отправка файла байтами (multipart). Соединение с Telegram из дата-центра моргает,
    поэтому таймаут на установку связи здесь щедрее, чем для обычных сообщений."""
    url = f"{API}/bot{config.TELEGRAM_BOT_TOKEN}/{method}"
    transport = httpx.AsyncHTTPTransport(local_address="0.0.0.0", retries=2)
    try:
        async with httpx.AsyncClient(transport=transport, timeout=httpx.Timeout(60, connect=20)) as client:
            r = await client.post(url, data=data, files=files)
        if r.status_code == 200:
            return True
        log.error("Telegram (%s, файлом) ответил %s: %s", method, r.status_code, r.text[:300])
    except Exception as e:  # noqa: BLE001
        log.warning("Telegram (%s, файлом) не удалось: %s: %s", method, type(e).__name__, e)
    return False


async def _fetch(photo_url: str) -> bytes | None:
    """Скачиваем картинку сами: файл лежит в хранилище того же дата-центра, это быстро.
    Если отдать Telegram только ссылку, он тянет файл со своей стороны и это занимает минуты."""
    try:
        async with _client() as client:
            r = await client.get(photo_url, timeout=httpx.Timeout(20, connect=8))
        if r.status_code == 200 and 0 < len(r.content) <= 9_000_000:
            return r.content
        log.warning("Картинку не забрали: статус %s, размер %d", r.status_code, len(r.content))
    except Exception as e:  # noqa: BLE001
        log.warning("Картинку не забрали: %s: %s", type(e).__name__, e)
    return None


async def send_photo(photo_url: str, caption: str = "", chat_id: str | None = None) -> bool:
    """Прислать изображение в Telegram. Быстрый путь: скачиваем файл сами (хранилище в том же
    дата-центре) и отправляем байтами, до трёх попыток — связь моргает."""
    chat_id = chat_id or config.TELEGRAM_CHAT_ID
    if not config.TELEGRAM_BOT_TOKEN or not chat_id:
        log.warning("Telegram не настроен: нет TELEGRAM_BOT_TOKEN или TELEGRAM_CHAT_ID")
        return False

    data = {"chat_id": chat_id}
    if caption:
        data["caption"] = caption[:1024]

    blob = await _fetch(photo_url)
    if blob:
        for attempt in (1, 2, 3):
            if await _upload("sendPhoto", data, {"photo": ("image.png", blob, "image/png")}):
                return True
            if attempt < 3:
                await asyncio.sleep(2 * attempt)
        return False

    # Файл не забрали (хранилище недоступно) — просим Telegram скачать самому. Он умеет это
    # не всегда: к хранилищу Timeweb ходит плохо и отвечает «failed to get HTTP URL content»,
    # поэтому здесь одна попытка, без долгих повторов.
    return await _call("sendPhoto", dict(data, photo=photo_url), attempts=1)


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
