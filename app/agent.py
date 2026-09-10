"""Вызов агента Timeweb по OpenAI-совместимому API — чтобы вечерний итог был написан его голосом."""
import logging

import httpx

from . import config

log = logging.getLogger("byt.agent")


async def ask(prompt: str) -> str | None:
    """Вернёт текст ответа агента или None, если агент не настроен / не ответил."""
    if not config.AGENT_API_URL or not config.AGENT_API_TOKEN:
        return None
    try:
        async with httpx.AsyncClient(timeout=90) as client:
            r = await client.post(
                config.AGENT_API_URL,
                headers={"Authorization": f"Bearer {config.AGENT_API_TOKEN}"},
                json={"messages": [{"role": "user", "content": prompt}], "stream": False},
            )
        r.raise_for_status()
        data = r.json()
        return data["choices"][0]["message"]["content"].strip()
    except Exception as e:  # noqa: BLE001
        log.error("Агент не ответил: %s", e)
        return None
