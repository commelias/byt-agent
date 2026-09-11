"""Часы: раз в минуту смотрим настройки и шлём то, что подошло по времени.

Расписание меняется агентом через set_setting без перезапуска сервиса.
reminders_sent гарантирует, что плановое напоминание уходит один раз в день;
delivery_log — журнал всего, что сервис прислал сам (его видит агент в context).
"""
import logging
from datetime import datetime, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from . import agent, config, db, orthodox, telegram

log = logging.getLogger("byt.scheduler")

DAY_CODES = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
KIND_RU = {"summary": "итог дня", "workout": "тренировка", "calendar": "календарь",
           "abstinence": "личный график", "nudge_meal": "вопрос о еде",
           "nudge_water": "вопрос о воде", "custom": "разовое напоминание"}


def _days(value: str) -> set[int]:
    return {DAY_CODES.index(x) for x in value.replace(" ", "").lower().split(",") if x in DAY_CODES}


def _hm(hhmm: str):
    try:
        h, m = map(int, hhmm.strip().split(":"))
        return h, m
    except (ValueError, AttributeError):
        return None


def _due(now: datetime, hhmm: str) -> bool:
    """Настроенное время наступило, но прошло не больше двух часов (догоняем после простоя,
    но не вываливаем все напоминания разом после позднего развёртывания)."""
    hm = _hm(hhmm)
    if not hm:
        return False
    delta = (now.hour * 60 + now.minute) - (hm[0] * 60 + hm[1])
    return 0 <= delta <= 120


def _at_today(now: datetime, hhmm: str, default: str) -> datetime:
    h, m = _hm(hhmm) or _hm(default)
    return now.replace(hour=h, minute=m, second=0, microsecond=0)


async def _send(kind: str, text: str) -> bool:
    ok = await telegram.send(text)
    db.log_delivery(KIND_RU.get(kind, kind), ok, "" if ok else "Telegram не принял сообщение")
    return ok


# ---------- плановые ----------

async def evening_summary(now: datetime, s: dict) -> str:
    day = now.date().isoformat()
    totals = db.day_totals(day)
    meals = db.meals_for_day(day)
    water = f"Вода: {db.water_total(day):.0f} из {s.get('water_norm_ml')} мл."
    if not meals:
        return "Итог дня: записей о еде сегодня не было. " + water
    plain = (f"Итог за {now.strftime('%d.%m')}: {totals['kcal']:.0f} ккал при норме {s['norm_kcal']}. "
             f"Б {totals['protein']:.0f}/{s['norm_protein']} · Ж {totals['fat']:.0f}/{s['norm_fat']} · "
             f"У {totals['carbs']:.0f}/{s['norm_carbs']}. {water}")
    detail = "\n".join(f"- {m['description']} — {float(m['kcal']):.0f} ккал" for m in meals)
    prompt = (f"Составь вечерний итог по питанию за сегодня — 3–5 строк, спокойно, без похвал и нотаций. "
              f"Данные:\n{detail}\n{plain}")
    return await agent.ask(prompt) or f"{plain}\n{detail}"


async def workout_reminder(now: datetime, s: dict) -> str:
    program = s.get("workout_program", "").strip()
    return "Сегодня тренировка по плану." + (f"\n{program}" if program else "")


async def calendar_reminder(now: datetime, s: dict) -> str | None:
    tomorrow = now.date() + timedelta(days=1)
    info = orthodox.describe(tomorrow)
    if not (info["fast"] or info["feast"]):
        return None
    return "Завтра — " + orthodox.human(tomorrow)


async def abstinence_reminder(now: datetime, s: dict) -> str:
    return s.get("abstinence_text") or "Сегодня день по графику."


async def planned(now: datetime, s: dict):
    day = now.date().isoformat()
    wd = now.weekday()
    checks = [
        ("summary", s.get("summary_time", ""), True, evening_summary),
        ("workout", s.get("workout_time", ""), wd in _days(s.get("workout_days", "")), workout_reminder),
        ("calendar", s.get("calendar_time", ""), True, calendar_reminder),
        ("abstinence", s.get("abstinence_time", ""), wd in _days(s.get("abstinence_days", "")), abstinence_reminder),
    ]
    for kind, hhmm, applies, handler in checks:
        if not applies or not _due(now, hhmm) or not db.reminder_claim(day, kind):
            continue
        try:
            text = await handler(now, s)
            if text is None:
                continue  # сегодня нечего сообщать (например, завтра не постный день)
            if not await _send(kind, text):
                db.reminder_unclaim(day, kind)  # следующий тик попробует снова
        except Exception as e:  # noqa: BLE001
            db.reminder_unclaim(day, kind)
            log.error("Ошибка напоминания %s: %s: %s", kind, type(e).__name__, e)


# ---------- разовые ----------

async def custom(now: datetime):
    stamp = now.strftime("%Y-%m-%d %H:%M")
    for r in db.pending_reminders():
        if r["at"] > stamp:
            break  # список отсортирован по времени
        ok = await _send("custom", "Напоминание: " + r["text"])
        overdue = datetime.strptime(r["at"], "%Y-%m-%d %H:%M").replace(tzinfo=now.tzinfo) < now - timedelta(hours=2)
        if ok or overdue:
            db.mark_reminder_sent(r["id"])


# ---------- забота: давно нет записей ----------

MEAL_TEXT = ("Давно не было записей о еде. Всё в порядке? Если ел — напиши, что было, я запишу. "
             "Если некогда — просто перекуси, это важнее записи.")
WATER_TEXT = "Давно не было отметок о воде. Выпей стакан и напиши сколько — я запишу."


async def nudges(now: datetime, s: dict):
    wake = _at_today(now, s.get("wake_time", ""), "08:00")
    sleep = _at_today(now, s.get("sleep_time", ""), "23:00")
    if not (wake + timedelta(hours=1) <= now <= sleep - timedelta(hours=1)):
        return  # до подъёма, сразу после, перед сном и ночью не беспокоим
    day = now.date().isoformat()
    strict = (orthodox.describe(now.date())["fast_reason"] or "").startswith("строгий")

    for kind, table, hours_key, cap, text, skip in (
        ("nudge_meal", "meals", "checkin_meal_hours", 2, MEAL_TEXT, strict),
        ("nudge_water", "water", "checkin_water_hours", 3, WATER_TEXT, False),
    ):
        try:
            hours = float(s.get(hours_key) or 0)
        except ValueError:
            hours = 0
        if hours <= 0 or skip:
            continue
        since = db.last_ts(table) or wake
        last_nudge = db.get_state(kind)
        if last_nudge:
            since = max(since, datetime.fromisoformat(last_nudge))
        if now - since < timedelta(hours=hours):
            continue
        if db.count_deliveries(day, KIND_RU[kind]) >= cap:
            continue
        db.set_state(kind, now.isoformat(timespec="minutes"))
        await _send(kind, text)


async def tick():
    now = db.now_local()
    s = db.get_settings()
    for step in (planned(now, s), custom(now), nudges(now, s)):
        try:
            await step
        except Exception as e:  # noqa: BLE001
            log.error("Ошибка в тике: %s: %s", type(e).__name__, e)


def start() -> AsyncIOScheduler:
    sched = AsyncIOScheduler(timezone=config.TZ)
    sched.add_job(tick, "interval", minutes=1, id="tick", max_instances=1, coalesce=True)
    sched.start()
    log.info("Планировщик запущен, зона %s", config.TZ_NAME)
    return sched
