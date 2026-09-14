"""Часы: раз в минуту смотрим расписание и складываем в очередь то, что подошло по времени.

Отправкой занимается один воркер (flush) — у него единственная логика повторов на весь сервис.
Раньше «повторить, если не ушло» было написано трижды и по-разному.
reminders_sent гарантирует, что плановое уходит один раз в день; delivery_log — журнал того,
что сервис прислал сам (его видит агент в context).
"""
import logging
from datetime import datetime, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from . import agent, config, db, orthodox, telegram

log = logging.getLogger("byt.scheduler")

MAX_ATTEMPTS = 5          # после этого сообщение помечается несостоявшимся и видно в /diag
BATCH = 6                 # сколько сообщений отправляем за один тик

KIND = {"summary": "итог дня", "workout": "тренировка", "calendar": "календарь",
        "abstinence": "личный график", "nudge_meal": "вопрос о еде",
        "nudge_water": "вопрос о воде", "custom": "разовое напоминание",
        "workout_check": "вопрос о тренировке", "plan": "напоминание"}


def _days(value: str) -> set[int]:
    return {db.DAY_CODES.index(x) for x in value.replace(" ", "").lower().split(",") if x in db.DAY_CODES}


def _hm(hhmm: str):
    try:
        h, m = map(int, hhmm.strip().split(":"))
        return h, m
    except (ValueError, AttributeError):
        return None


def _due(now: datetime, hhmm: str, window: int = 120) -> bool:
    """Время наступило, но прошло не больше окна (догоняем после простоя, но не вываливаем
    все напоминания разом после позднего развёртывания)."""
    hm = _hm(hhmm)
    if not hm:
        return False
    delta = (now.hour * 60 + now.minute) - (hm[0] * 60 + hm[1])
    return 0 <= delta <= window


def _at_today(now: datetime, hhmm: str, default: str) -> datetime:
    h, m = _hm(hhmm) or _hm(default)
    return now.replace(hour=h, minute=m, second=0, microsecond=0)


# ---------- что именно сказать ----------

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
    prompt = ("Составь вечерний итог по питанию за сегодня — 3–5 строк, спокойно, без похвал и нотаций. "
              f"Данные:\n{detail}\n{plain}")
    return await agent.ask(prompt) or f"{plain}\n{detail}"


async def workout_reminder(now: datetime, s: dict) -> str | None:
    planned, why = db.workout_planned(now.date().isoformat(), s.get("workout_days", ""))
    if not planned:
        return None
    head = "Сегодня тренировка по плану." if not why else f"Сегодня тренировка ({why})."
    program = s.get("workout_program", "").strip()
    return head + (f"\n{program}" if program else "")


async def calendar_reminder(now: datetime, s: dict) -> str | None:
    tomorrow = now.date() + timedelta(days=1)
    info = orthodox.describe(tomorrow)
    if not (info["fast"] or info["feast"]):
        return None
    return "Завтра — " + orthodox.human(tomorrow)


async def abstinence_reminder(now: datetime, s: dict) -> str:
    return s.get("abstinence_text") or "Сегодня день по графику."


async def workout_check(now: datetime, s: dict) -> str | None:
    """Вечером спросить о тренировке, если она была запланирована на сегодня и не записана.
    Смотрит исключения, поэтому перенос на выходной не теряется."""
    day = now.date().isoformat()
    planned, why = db.workout_planned(day, s.get("workout_days", ""))
    if not planned or db.workout_on(day):
        return None
    hint = f" (перенос: {why})" if why else ""
    return f"Тренировка на сегодня в плане{hint}, а записи нет. Была? Как колено?"


# ---------- плановые ----------

async def planned(now: datetime, s: dict):
    day = now.date().isoformat()
    wd = now.weekday()
    checks = [
        ("summary", s.get("summary_time", ""), True, evening_summary),
        ("workout", s.get("workout_time", ""), True, workout_reminder),
        ("calendar", s.get("calendar_time", ""), True, calendar_reminder),
        ("abstinence", s.get("abstinence_time", ""), wd in _days(s.get("abstinence_days", "")), abstinence_reminder),
        ("workout_check", s.get("workout_check_time", ""), True, workout_check),
    ]
    for kind, hhmm, applies, handler in checks:
        if not applies or not _due(now, hhmm) or not db.reminder_claim(day, kind):
            continue
        try:
            text = await handler(now, s)
            if text:
                db.enqueue(KIND[kind], text)
        except Exception as e:  # noqa: BLE001
            log.error("Ошибка напоминания %s: %s: %s", kind, type(e).__name__, e)


async def user_plans(now: datetime):
    """Повторяющиеся напоминания, заведённые агентом: текстом или ссылкой на точный текст."""
    day = now.date().isoformat()
    for p in db.list_plans(only_enabled=True):
        days = (p["days"] or "").strip()
        if days and days != "all" and now.weekday() not in _days(days):
            continue
        if not _due(now, p["at"]) or not db.reminder_claim(day, f"plan{p['id']}"):
            continue
        body = (p["body"] or "").strip()
        if p["text_key"]:
            saved = db.get_text(p["text_key"])
            if not saved:
                log.error("План #%s ссылается на текст «%s», которого нет", p["id"], p["text_key"])
                continue
            title = (saved["title"] or p["title"]).strip()
            body = (f"{title}\n\n" if title else "") + saved["body"]
        if body:
            db.enqueue(KIND["plan"], body)


# ---------- разовые ----------

async def custom(now: datetime):
    stamp = now.strftime("%Y-%m-%d %H:%M")
    for r in db.pending_reminders():
        if r["at"] > stamp:
            break  # список отсортирован по времени
        db.enqueue(KIND["custom"], "Напоминание: " + r["text"])
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
        if db.count_deliveries(day, KIND[kind]) >= cap:
            continue
        db.enqueue(KIND[kind], text)
        db.set_state(kind, now.isoformat(timespec="minutes"))


# ---------- единственный отправщик ----------

async def flush(now: datetime):
    """Забрать очередь и отправить. Здесь же повторы: сеть до Telegram моргает регулярно."""
    for item in db.outbox_pending(limit=BATCH):
        ok = False
        try:
            ok = await telegram.send(item["text"])
        except Exception as e:  # noqa: BLE001
            log.error("Отправка сорвалась: %s: %s", type(e).__name__, e)
        if ok:
            db.outbox_done(item["id"])
            db.log_delivery(item["kind"], True)
            continue
        attempts = int(item["attempts"]) + 1
        if attempts >= MAX_ATTEMPTS:
            db.outbox_give_up(item["id"], attempts, "Telegram не принял сообщение")
            db.log_delivery(item["kind"], False, f"не доставлено за {attempts} попыток")
        else:
            nxt = (now + timedelta(minutes=3 * attempts)).isoformat(timespec="minutes")
            db.outbox_retry(item["id"], attempts, nxt, "Telegram не принял сообщение")


async def tick():
    now = db.now_local()
    s = db.get_settings()
    for step in (planned(now, s), user_plans(now), custom(now), nudges(now, s), flush(now)):
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
