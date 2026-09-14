"""Один исходящий HTTP-клиент на весь сервис: повторы, таймауты, IPv4.

Раньше своя обвязка была в telegram.py и своя в agent.py. Связь дата-центра с внешним
миром периодически моргает, и правило одно для всех: несколько попыток с растущей паузой.
"""
import asyncio
import logging

import httpx

log = logging.getLogger("byt.http")
# httpx печатает полный адрес запроса, а в нём бывает токен — в логах ему не место
logging.getLogger("httpx").setLevel(logging.WARNING)


def client(timeout: float = 30, connect: float = 8, retries: int = 1) -> httpx.AsyncClient:
    """Только IPv4 (local_address 0.0.0.0): из дата-центра путь по IPv6 бывает недоступен,
    и соединение висит до таймаута."""
    transport = httpx.AsyncHTTPTransport(local_address="0.0.0.0", retries=retries)
    return httpx.AsyncClient(transport=transport, timeout=httpx.Timeout(timeout, connect=connect))


async def post_json(url: str, payload: dict, *, attempts: int = 4, timeout: float = 30,
                    connect: float = 8, headers: dict | None = None, label: str = "") -> dict | None:
    """Вернёт разобранный ответ или None. Ошибка сети — повтор, ошибка сервера — сразу None:
    повторять запрос, который сервер понял и отверг, бессмысленно."""
    label = label or url.split("/")[-1]
    last = None
    for attempt in range(1, attempts + 1):
        try:
            async with client(timeout=timeout, connect=connect) as c:
                r = await c.post(url, json=payload, headers=headers)
            if r.status_code == 200:
                return r.json()
            log.error("%s ответил %s: %s", label, r.status_code, r.text[:300])
            return None
        except Exception as e:  # noqa: BLE001
            last = e
            log.warning("%s, попытка %d: %s: %s", label, attempt, type(e).__name__, e or "(без текста)")
            if attempt < attempts:
                await asyncio.sleep(3 * attempt)
    log.error("%s недоступен после %d попыток: %s: %s", label, attempts, type(last).__name__, last)
    return None
