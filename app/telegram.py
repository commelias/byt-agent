"""Отправка сообщений в Telegram напрямую через Bot API (агент сам писать первым не умеет)."""
import logging

from . import config, http

log = logging.getLogger("byt.telegram")

API = "https://api.telegram.org"
LIMIT = 3500  # у Telegram потолок 4096 знаков; режем с запасом


def _split(text: str) -> list[str]:
    """Длинный текст — на части по границам абзацев и строк, а не посреди слова.
    Молитвенное правило или программа тренировок в один вызов не помещаются."""
    parts, rest = [], text.strip()
    while len(rest) > LIMIT:
        window = rest[:LIMIT]
        cut = max(window.rfind("\n\n"), window.rfind("\n"))
        if cut < LIMIT // 3:
            cut = window.rfind(" ")
        if cut <= 0:
            cut = LIMIT
        parts.append(rest[:cut].rstrip())
        rest = rest[cut:].lstrip()
    if rest:
        parts.append(rest)
    return parts or [""]


async def send(text: str, chat_id: str | None = None) -> bool:
    """Прислать сообщение. Длинный текст уходит несколькими частями по порядку."""
    chat_id = chat_id or config.TELEGRAM_CHAT_ID
    if not config.TELEGRAM_BOT_TOKEN or not chat_id:
        log.warning("Telegram не настроен: нет TELEGRAM_BOT_TOKEN или TELEGRAM_CHAT_ID")
        return False
    url = f"{API}/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage"
    parts = _split(text)
    for i, part in enumerate(parts, 1):
        suffix = f"\n\n({i} из {len(parts)})" if len(parts) > 1 else ""
        if await http.post_json(url, {"chat_id": chat_id, "text": part + suffix},
                                label="Telegram sendMessage") is None:
            return False
    return True


async def diagnose() -> str:
    """Что видно из контейнера: DNS и ответ getMe. Для страницы /diag."""
    import socket
    out = []
    try:
        out.append(f"dns api.telegram.org -> {socket.gethostbyname('api.telegram.org')}")
    except Exception as e:  # noqa: BLE001
        out.append(f"dns fail: {type(e).__name__}: {e}")
    try:
        async with http.client() as client:
            r = await client.get(f"{API}/bot{config.TELEGRAM_BOT_TOKEN}/getMe")
        out.append(f"getMe -> {r.status_code} {r.text[:120]}")
    except Exception as e:  # noqa: BLE001
        out.append(f"getMe fail: {type(e).__name__}: {e}")
    return "\n".join(out)
