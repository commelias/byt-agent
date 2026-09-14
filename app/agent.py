"""Вызов агента Timeweb по OpenAI-совместимому API — чтобы вечерний итог был написан его голосом."""
import logging

from . import config, http

log = logging.getLogger("byt.agent")


async def ask(prompt: str) -> str | None:
    """Вернёт текст ответа агента или None, если агент не настроен или не ответил."""
    if not config.AGENT_API_URL or not config.AGENT_API_TOKEN:
        return None
    data = await http.post_json(
        config.AGENT_API_URL,
        {"messages": [{"role": "user", "content": prompt}], "stream": False},
        attempts=2, timeout=90, connect=15, label="агент Timeweb",
        headers={"Authorization": f"Bearer {config.AGENT_API_TOKEN}"},
    )
    try:
        return data["choices"][0]["message"]["content"].strip()
    except (TypeError, KeyError, IndexError):
        return None
