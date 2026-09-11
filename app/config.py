"""Настройки сервиса. Всё берётся из переменных окружения App Platform."""
import os
from zoneinfo import ZoneInfo

# --- обязательные ---
# Токен Telegram-бота (тот же, что стоит в канале агента Timeweb)
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
# Числовой ID пользователя Telegram, которому бот пишет напоминания
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
# Секрет, которым закрыт MCP-сервер. Этот же токен вписывается в панели агента
MCP_TOKEN = os.environ.get("MCP_TOKEN", "")

# --- база данных ---
# postgresql://user:pass@host:5432/db  — managed PostgreSQL Timeweb
# если не задано — SQLite-файл (только для локальной проверки)
DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///byt.db")

# --- агент Timeweb (не обязательно) ---
# Если заданы — вечерний итог формулирует агент, иначе шлётся сухая сводка.
# OpenAI-совместимый адрес: https://agent.timeweb.cloud/api/v1/cloud-ai/agents/{id}/v1/chat/completions
AGENT_API_URL = os.environ.get("AGENT_API_URL", "")
AGENT_API_TOKEN = os.environ.get("AGENT_API_TOKEN", "")

# --- прочее ---
TZ_NAME = os.environ.get("TZ", "Europe/Moscow")
TZ = ZoneInfo(TZ_NAME)
PORT = int(os.environ.get("PORT", "8080"))

# Настройки по умолчанию; живут в таблице settings и меняются агентом через MCP
DEFAULT_SETTINGS = {
    "norm_kcal": "2000",
    "norm_protein": "120",
    "norm_fat": "70",
    "norm_carbs": "220",
    "summary_time": "21:00",          # вечерний итог по питанию
    "workout_days": "mon,wed,fri",    # дни тренировок
    "workout_time": "08:00",          # напоминание о тренировке
    "workout_program": "",            # текст программы (свободная форма)
    "calendar_time": "20:00",         # напоминание «завтра постный день»
    "abstinence_days": "",            # личный график, напр. "mon,wed,fri"; пусто — выключено
    "abstinence_time": "09:00",
    "abstinence_text": "Сегодня день по графику.",
    # режим дня: вне этого окна сервис сам не беспокоит
    "wake_time": "08:00",
    "sleep_time": "23:00",
    # забота: через сколько часов без записей спросить (0 — не спрашивать)
    "checkin_meal_hours": "5",
    "checkin_water_hours": "3",
    "water_norm_ml": "2000",
}
