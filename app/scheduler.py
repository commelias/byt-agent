"""Часы: раз в минуту смотрим настройки и шлём то, что подошло по времени.

Так расписание меняется агентом через set_setting без перезапуска сервиса,
а таблица reminders_sent гарантирует, что одно напоминание за день уходит один раз.
"""
import logging
from datetime import date, datetime, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from . import agent, config, db, orthodox, telegram

log = logging.getLogger("byt.scheduler")

DAY_CODES = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def _days(value: str) -> set[int]:
    return {DAY_CODES.index(x) for x in value.replace(" ", "").lower().split(",") if x in DAY_CODES}


def _due(now: datetime, hhmm: str) -> bool:
    """Пора ли: настроенное время уже наступило, но прошло не больше двух часов.
    Окно нужно, чтобы после простоя сервиса напоминание догнало, а после развёртывания
    поздно вечером не вылетели все напоминания за день разом."""
    try:
        h, m = map(int, hhmm.split(":"))
    except ValueError:
        return False
    delta = (now.hour * 60 + now.minute) - (h * 60 + m)
    return 0 <= delta <= 120


async def evening_summary(now: datetime, s: dict):
    day = now.date().isoformat()
    totals = db.day_totals(day)
    meals = db.meals_for_day(day)
    if not meals:
        text = "Итог дня: записей о еде сегодня не было."
    else:
        plain = (f"Итог за {now.strftime('%d.%m')}: {totals['kcal']:.0f} ккал при норме {s['norm_kcal']}. "
                 f"Б {totals['protein']:.0f}/{s['norm_protein']} · Ж {totals['fat']:.0f}/{s['norm_fat']} · "
                 f"У {totals['carbs']:.0f}/{s['norm_carbs']}.")
        detail = "\n".join(f"- {m['description']} — {float(m['kcal']):.0f} ккал" for m in meals)
        prompt = (f"Составь вечерний итог по питанию за сегодня — 3–5 строк, спокойно, без похвал и нотаций. "
                  f"Данные:\n{detail}\n{plain}")
        text = await agent.ask(prompt) or f"{plain}\n{detail}"
    await telegram.send(text)


async def workout_reminder(now: datetime, s: dict):
    program = s.get("workout_program", "").strip()
    text = "Сегодня тренировка." + (f"\n{program}" if program else "")
    await telegram.send(text)


async def calendar_reminder(now: datetime, s: dict):
    tomorrow = now.date() + timedelta(days=1)
    info = orthodox.describe(tomorrow)
    if not (info["fast"] or info["feast"]):
        return
    await telegram.send("Завтра — " + orthodox.human(tomorrow))


async def abstinence_reminder(now: datetime, s: dict):
    await telegram.send(s.get("abstinence_text") or "Сегодня день по графику.")


async def tick():
    now = db.now_local()
    day = now.date().isoformat()
    s = db.get_settings()
    wd = now.weekday()

    checks = [
        ("summary", s.get("summary_time", ""), True, evening_summary),
        ("workout", s.get("workout_time", ""), wd in _days(s.get("workout_days", "")), workout_reminder),
        ("calendar", s.get("calendar_time", ""), True, calendar_reminder),
        ("abstinence", s.get("abstinence_time", ""), wd in _days(s.get("abstinence_days", "")), abstinence_reminder),
    ]
    for kind, hhmm, applies, handler in checks:
        if not applies or not hhmm or not _due(now, hhmm):
            continue
        if not db.reminder_claim(day, kind):
            continue
        try:
            await handler(now, s)
            log.info("Отправлено: %s (%s)", kind, day)
        except Exception as e:  # noqa: BLE001
            log.error("Ошибка напоминания %s: %s", kind, e)


def start() -> AsyncIOScheduler:
    sched = AsyncIOScheduler(timezone=config.TZ)
    sched.add_job(tick, "interval", minutes=1, id="tick", max_instances=1, coalesce=True)
    sched.start()
    log.info("Планировщик запущен, зона %s", config.TZ_NAME)
    return sched
