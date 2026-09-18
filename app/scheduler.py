"""Часы: раз в минуту проходим по событиям и складываем в очередь то, что подошло.

Событие — единица планировщика, и системные, и заведённые человеком лежат в одной таблице.
У события четыре стрелки:
    КОГДА      — at, days, repeat_hours, max_per_day
    УСЛОВИЕ    — cond и param: пусто (просто по времени) либо имя из реестра CONDITIONS
    ДОКУМЕНТ   — doc_kind: text (текст в событии), saved (точный текст из texts), calc (обработчик)
    ОТМЕТКА    — mark: что записать в журнал, когда человек ответит

Отправкой и повторами занимается один воркер (flush) — единственная логика повторов на весь сервис.
"""
import logging
from datetime import datetime, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from . import agent, config, db, orthodox, telegram

log = logging.getLogger("byt.scheduler")

MAX_ATTEMPTS = 5   # после этого сообщение помечается несостоявшимся; Донна передаст его сама (context)
BATCH = 6          # сколько сообщений отправляем за один тик

MEAL_TEXT = ("Давно не было записей о еде. Всё в порядке? Если ел — напиши, что было, я запишу. "
             "Если некогда — просто перекуси, это важнее записи.")
WATER_TEXT = "Давно не было отметок о воде. Выпей стакан и напиши сколько — я запишу."

# Системные события. Заводятся один раз при первом старте; дальше время и дни принадлежат человеку.
SYSTEM_EVENTS = [
    dict(ekey="summary", title="Итог дня", at="21:00", doc_kind="calc", doc="summary"),
    dict(ekey="workout", title="Тренировка утром", at="08:00", cond="workout_planned",
         doc_kind="calc", doc="workout"),
    dict(ekey="workout_check", title="Вечерний вопрос о тренировке", at="20:30",
         cond="workout_unlogged", doc_kind="calc", doc="workout_check", mark="тренировка"),
    dict(ekey="calendar", title="Завтра постный день", at="20:00", cond="fast_tomorrow",
         doc_kind="calc", doc="calendar"),
    dict(ekey="abstinence", title="Личный график", at="09:00", days="", doc_kind="calc",
         doc="abstinence", enabled=False),
    dict(ekey="meal_check", title="Вопрос о еде", cond="no_meals", param=5,
         doc_kind="text", doc=MEAL_TEXT, mark="еда", repeat_hours=5, max_per_day=2),
    dict(ekey="water_check", title="Вопрос о воде", cond="no_water", param=3,
         doc_kind="text", doc=WATER_TEXT, mark="вода", repeat_hours=3, max_per_day=3),
]


def seed():
    for e in SYSTEM_EVENTS:
        spec = dict(e, system=True)
        enabled = spec.pop("enabled", True)
        if db.seed_event(**spec) and not enabled:
            row = db.get_event(spec["ekey"])
            db.update_event(row["id"], enabled=0)


# ---------- когда ----------

def _days(value: str) -> set[int]:
    return {db.DAY_CODES.index(x) for x in value.replace(" ", "").lower().split(",") if x in db.DAY_CODES}


def _hm(hhmm: str):
    try:
        h, m = map(int, hhmm.strip().split(":"))
        return h, m
    except (ValueError, AttributeError):
        return None


def _due(now: datetime, hhmm: str, window: int = 120) -> bool:
    """Время наступило, но прошло не больше окна: догоняем после простоя, но не вываливаем
    всё разом после позднего развёртывания."""
    hm = _hm(hhmm)
    if not hm:
        return False
    delta = (now.hour * 60 + now.minute) - (hm[0] * 60 + hm[1])
    return 0 <= delta <= window


def _at_today(now: datetime, hhmm: str, default: str) -> datetime:
    h, m = _hm(hhmm) or _hm(default)
    return now.replace(hour=h, minute=m, second=0, microsecond=0)


def _awake(now: datetime, s: dict) -> bool:
    """Час после подъёма и час до отбоя — тишина."""
    wake = _at_today(now, s.get("wake_time", ""), "08:00")
    sleep = _at_today(now, s.get("sleep_time", ""), "23:00")
    return wake + timedelta(hours=1) <= now <= sleep - timedelta(hours=1)


# ---------- условия ----------

def _quiet_since(now: datetime, table: str, ekey: str, s: dict) -> float:
    """Сколько часов нет записей — с оглядкой на то, когда мы спрашивали в прошлый раз."""
    since = db.last_ts(table) or _at_today(now, s.get("wake_time", ""), "08:00")
    asked = db.get_state(ekey)
    if asked:
        since = max(since, datetime.fromisoformat(asked))
    return (now - since).total_seconds() / 3600


def _cond(name: str, ev: dict, now: datetime, s: dict) -> bool:
    day = now.date().isoformat()
    if not name:
        return True
    if name == "workout_planned":
        return db.workout_planned(day, s.get("workout_days", ""))[0]
    if name == "workout_unlogged":
        return db.workout_planned(day, s.get("workout_days", ""))[0] and not db.workout_on(day)
    if name == "fast_tomorrow":
        info = orthodox.describe(now.date() + timedelta(days=1))
        return bool(info["fast"] or info["feast"])
    if name == "no_meals":
        strict = (orthodox.describe(now.date())["fast_reason"] or "").startswith("строгий")
        return not strict and _awake(now, s) and _quiet_since(now, "meals", ev["ekey"], s) >= ev["param"]
    if name == "no_water":
        return _awake(now, s) and _quiet_since(now, "water", ev["ekey"], s) >= ev["param"]
    log.error("Событие %s ссылается на неизвестное условие «%s»", ev["ekey"], name)
    return False


# ---------- документы ----------

async def _calc(name: str, now: datetime, s: dict) -> str | None:
    if name == "summary":
        return await _summary(now, s)
    if name == "workout":
        _, why = db.workout_planned(now.date().isoformat(), s.get("workout_days", ""))
        head = f"Сегодня тренировка ({why})." if why else "Сегодня тренировка по плану."
        program = s.get("workout_program", "").strip()
        return head + (f"\n{program}" if program else "")
    if name == "workout_check":
        _, why = db.workout_planned(now.date().isoformat(), s.get("workout_days", ""))
        return f"Тренировка на сегодня в плане{f' ({why})' if why else ''}, а записи нет. Была? Как колено?"
    if name == "calendar":
        return "Завтра — " + orthodox.human(now.date() + timedelta(days=1))
    if name == "abstinence":
        return s.get("abstinence_text") or "Сегодня день по графику."
    log.error("Неизвестный обработчик документа «%s»", name)
    return None


async def _summary(now: datetime, s: dict) -> str:
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


async def _document(ev: dict, now: datetime, s: dict) -> str | None:
    kind = ev["doc_kind"]
    if kind == "text":
        return ev["doc"].strip() or None
    if kind == "saved":
        saved = db.get_text(ev["doc"])
        if not saved:
            log.error("Событие %s ссылается на текст «%s», которого нет", ev["ekey"], ev["doc"])
            return None
        title = (saved["title"] or ev["title"]).strip()
        return (f"{title}\n\n" if title else "") + saved["body"]
    if kind == "calc":
        return await _calc(ev["doc"], now, s)
    log.error("Событие %s: неизвестный вид документа «%s»", ev["ekey"], kind)
    return None


# ---------- проход по событиям ----------

async def events(now: datetime, s: dict):
    day = now.date().isoformat()
    for ev in db.list_events(only_enabled=True):
        try:
            days = (ev["days"] or "").strip()
            if days and days != "all" and now.weekday() not in _days(days):
                continue
            repeating = float(ev["repeat_hours"] or 0) > 0
            if repeating:
                if db.count_deliveries(day, ev["title"]) >= int(ev["max_per_day"]):
                    continue
            elif not _due(now, ev["at"]):
                continue
            if not _cond(ev["cond"], ev, now, s):
                continue
            if not repeating and not db.reminder_claim(day, "ev:" + ev["ekey"]):
                continue
            text = await _document(ev, now, s)
            if not text:
                continue
            db.enqueue(ev["title"], text)
            if repeating:
                db.set_state(ev["ekey"], now.isoformat(timespec="minutes"))
            if ev["mark"]:
                db.set_state("ждёт:" + ev["mark"], day)
        except Exception as e:  # noqa: BLE001
            log.error("Событие %s сорвалось: %s: %s", ev["ekey"], type(e).__name__, e)


async def custom(now: datetime):
    """Разовые напоминания живут отдельно: у них нет ни условия, ни документа — только время."""
    stamp = now.strftime("%Y-%m-%d %H:%M")
    for r in db.pending_reminders():
        if r["at"] > stamp:
            break  # список отсортирован по времени
        db.enqueue("разовое напоминание", "Напоминание: " + r["text"])
        db.mark_reminder_sent(r["id"])


# ---------- единственный отправщик ----------

async def flush(now: datetime):
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
        break  # Telegram недоступен — остальных в этот тик не мучаем, тик не должен висеть


async def tick():
    now = db.now_local()
    s = db.get_settings()
    for step in (events(now, s), custom(now), flush(now)):
        try:
            await step
        except Exception as e:  # noqa: BLE001
            log.error("Ошибка в тике: %s: %s", type(e).__name__, e)


def start() -> AsyncIOScheduler:
    seed()
    sched = AsyncIOScheduler(timezone=config.TZ)
    sched.add_job(tick, "interval", minutes=1, id="tick", max_instances=1, coalesce=True)
    sched.start()
    log.info("Планировщик запущен, зона %s", config.TZ_NAME)
    return sched
