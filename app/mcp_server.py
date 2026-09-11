"""MCP-сервер «Журнал» — руки агента: записать, прочитать, настроить."""
from datetime import date, datetime, timedelta

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from . import config, db, orthodox

mcp = FastMCP(
    # Сервер живёт за публичным доменом App Platform, а не на localhost;
    # доступ и так закрыт Bearer-токеном в main.py, поэтому проверку Host отключаем.
    "byt-journal",
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    instructions=(
        "Журнал быта одного человека: питание, вода, тренировки, православный календарь, настройки, "
        "заметки о человеке и разовые напоминания. Перед ответом на каждое сообщение вызывай context — "
        "это текущее время и память. Записывай каждый приём пищи, воду и тренировку сразу."
    ),
    stateless_http=True,
    json_response=True,
    streamable_http_path="/mcp",
)


def _fmt(x) -> str:
    return "—" if x is None else f"{float(x):.0f}"


def _totals_line(t: dict, s: dict) -> str:
    return (f"Итого: {t['kcal']:.0f} ккал (норма {s.get('norm_kcal')}), "
            f"Б {t['protein']:.0f}/{s.get('norm_protein')} · Ж {t['fat']:.0f}/{s.get('norm_fat')} · "
            f"У {t['carbs']:.0f}/{s.get('norm_carbs')}")


# ---------- питание ----------

@mcp.tool()
def log_meal(description: str, kcal: float, grams: float = 0,
             protein: float = 0, fat: float = 0, carbs: float = 0,
             day: str = "") -> str:
    """Записать приём пищи в дневник. description — блюдо и состав, kcal — калории,
    grams — вес порции, protein/fat/carbs — БЖУ в граммах. day — дата YYYY-MM-DD, по умолчанию сегодня.
    Возвращает итог за день."""
    db.add_meal(description, kcal, grams, protein, fat, carbs, day)
    return f"Записано: {description} — {kcal:.0f} ккал.\n" + _totals_line(db.day_totals(day), db.get_settings())


@mcp.tool()
def day_summary(day: str = "") -> str:
    """Итог питания за день: список приёмов пищи и суммы калорий и БЖУ против норм.
    day — дата YYYY-MM-DD, по умолчанию сегодня."""
    meals = db.meals_for_day(day)
    if not meals:
        return f"За {day or db.today()} записей о еде нет."
    lines = [f"{m['ts'][11:16]} {m['description']} — {_fmt(m['grams'])} г, {_fmt(m['kcal'])} ккал, "
             f"Б {_fmt(m['protein'])} / Ж {_fmt(m['fat'])} / У {_fmt(m['carbs'])}" for m in meals]
    return "\n".join(lines) + "\n" + _totals_line(db.day_totals(day), db.get_settings())


@mcp.tool()
def undo_last_meal(day: str = "") -> str:
    """Удалить последнюю запись о еде за день (если ошиблись или записали дважды)."""
    removed = db.delete_last_meal(day)
    return f"Удалено: {removed}" if removed else "Удалять нечего — записей за день нет."


@mcp.tool()
def meals_history(days: int = 7) -> str:
    """Сводка питания по дням за последние N дней: калории и БЖУ за каждый день."""
    rows = db.meals_history(days)
    if not rows:
        return "История пуста."
    return "\n".join(f"{r['day']}: {float(r['kcal'] or 0):.0f} ккал, Б {float(r['protein'] or 0):.0f} / "
                     f"Ж {float(r['fat'] or 0):.0f} / У {float(r['carbs'] or 0):.0f} ({r['n']} записей)" for r in rows)


# ---------- спорт ----------

@mcp.tool()
def log_workout(description: str, feeling: str = "", note: str = "",
                day: str = "") -> str:
    """Записать выполненную тренировку. description — что сделано (упражнения, подходы, веса),
    feeling — самочувствие/тяжесть по словам человека, note — замечания (боль, пропуск упражнения)."""
    db.add_workout(description, feeling, note, day)
    return "Тренировка записана."


@mcp.tool()
def workouts_history(count: int = 10) -> str:
    """Последние N тренировок с датами и самочувствием."""
    rows = db.workouts_history(count)
    if not rows:
        return "Тренировок в журнале нет."
    out = []
    for r in rows:
        line = f"{r['day']}: {r['description']}"
        if r["feeling"]:
            line += f" · самочувствие: {r['feeling']}"
        if r["note"]:
            line += f" · {r['note']}"
        out.append(line)
    return "\n".join(out)


@mcp.tool()
def workout_plan() -> str:
    """Программа и расписание тренировок из настроек."""
    s = db.get_settings()
    return (f"Дни: {s.get('workout_days')}, напоминание в {s.get('workout_time')}.\n"
            f"Программа:\n{s.get('workout_program') or '(не задана — спроси и сохрани через set_setting workout_program)'}")


# ---------- календарь ----------

@mcp.tool()
def today_calendar(day: str = "") -> str:
    """Точные сведения о дне по православному календарю: постный ли, почему, праздник ли,
    сплошная седмица ли. Плюс то же для завтра. day — дата YYYY-MM-DD, по умолчанию сегодня.
    Всегда используй этот инструмент вместо собственной памяти о датах."""
    d = date.fromisoformat(day) if day else db.now_local().date()
    return "Сегодня — " + orthodox.human(d) + "\nЗавтра — " + orthodox.human(d + timedelta(days=1))


@mcp.tool()
def log_calendar_mark(kind: str, note: str = "", day: str = "") -> str:
    """Отметить в журнале: kind — 'пост_соблюдён', 'пост_нарушен', 'график_соблюдён', 'график_нарушен'
    или свободное слово. note — что человек сказал, коротко и без оценок."""
    db.add_mark(kind, note, day)
    return "Отмечено."


@mcp.tool()
def calendar_marks_history(count: int = 14) -> str:
    """Последние отметки по календарю (посты, личный график)."""
    rows = db.marks_history(count)
    if not rows:
        return "Отметок нет."
    return "\n".join(f"{r['day']}: {r['kind']}" + (f" — {r['note']}" if r["note"] else "") for r in rows)


# ---------- настройки ----------

@mcp.tool()
def get_settings() -> str:
    """Все настройки: нормы калорий и БЖУ, время вечернего итога, дни и время тренировок,
    программа, время напоминания о календаре, личный график (дни, время, текст)."""
    s = db.get_settings()
    return "\n".join(f"{k} = {v}" for k, v in sorted(s.items()))


@mcp.tool()
def set_setting(key: str, value: str) -> str:
    """Изменить настройку. Ключи: norm_kcal, norm_protein, norm_fat, norm_carbs,
    summary_time (HH:MM), workout_days (mon,tue,wed,thu,fri,sat,sun через запятую), workout_time,
    workout_program (текст), calendar_time, abstinence_days, abstinence_time, abstinence_text,
    wake_time и sleep_time (HH:MM, режим дня), checkin_meal_hours и checkin_water_hours
    (через сколько часов без записей спросить о еде/воде; 0 — не спрашивать), water_norm_ml.
    Пустая строка в abstinence_days выключает личный график."""
    if key not in config.DEFAULT_SETTINGS:
        return f"Неизвестный ключ {key}. Допустимые: {', '.join(config.DEFAULT_SETTINGS)}"
    db.set_setting(key, value.strip())
    return f"{key} = {value.strip()}"


# ---------- часы и память ----------

WEEKDAYS = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]


def _part_of_day(h: int) -> str:
    if 5 <= h < 11:
        return "утро"
    if 11 <= h < 17:
        return "день"
    if 17 <= h < 22:
        return "вечер"
    return "ночь"


@mcp.tool()
def context() -> str:
    """ВЫЗЫВАЙ ПЕРЕД КАЖДЫМ ОТВЕТОМ. Текущие дата, время и время суток; календарь на сегодня и завтра;
    что съедено и выпито сегодня и когда в последний раз; последняя тренировка; заметки о человеке
    (его просьбы и привычки, которые надо соблюдать); запланированные напоминания; что сервис уже
    прислал сегодня сам."""
    now = db.now_local()
    s = db.get_settings()
    day = now.date()
    out = [f"Сейчас: {WEEKDAYS[now.weekday()]}, {now.strftime('%d.%m.%Y %H:%M')} ({_part_of_day(now.hour)}). "
           f"Режим дня: подъём {s.get('wake_time')}, отбой {s.get('sleep_time')}."]
    out.append("Календарь: сегодня — " + orthodox.human(day) + "; завтра — " + orthodox.human(day + timedelta(days=1)))

    t = db.day_totals()
    last_meal = db.last_ts("meals")
    meal = f"Еда сегодня: {t['kcal']:.0f} из {s.get('norm_kcal')} ккал, записей {t['count']}"
    meal += f", последняя в {last_meal.strftime('%H:%M')}." if last_meal else ", записей пока нет."
    out.append(meal)
    last_water = db.last_ts("water")
    water = f"Вода сегодня: {db.water_total():.0f} из {s.get('water_norm_ml')} мл"
    water += f", последняя отметка в {last_water.strftime('%H:%M')}." if last_water else ", отметок нет."
    out.append(water)

    codes = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
    planned = codes[now.weekday()] in s.get("workout_days", "")
    lw = db.last_workout()
    out.append(f"Тренировка сегодня по плану: {'да' if planned else 'нет'}. "
               f"Последняя записанная: {lw['day'] + ' — ' + lw['description'] if lw else 'нет'}.")

    notes = db.list_notes()
    if notes:
        out.append("Заметки о человеке (соблюдай):\n" + "\n".join(f"  #{n['id']} {n['text']}" for n in notes))
    rem = db.pending_reminders()
    if rem:
        out.append("Запланированные напоминания:\n" + "\n".join(f"  #{r['id']} {r['at']} — {r['text']}" for r in rem))
    sent = [d for d in db.deliveries(day.isoformat()) if d["ok"]]
    if sent:
        out.append("Сервис сегодня уже прислал сам: " + ", ".join(f"{d['ts'][11:16]} {d['kind']}" for d in sent) + ".")
    return "\n".join(out)


@mcp.tool()
def remember(note: str) -> str:
    """Запомнить надолго просьбу или привычку человека: как к нему обращаться, чего не делать,
    что он любит, особенности режима. Всё запомненное возвращается в context при каждом ответе.
    Вызывай, когда человек просит что-то «запомнить» или меняет правила общения."""
    db.add_note(note.strip())
    return f"Запомнено: {note.strip()}"


@mcp.tool()
def forget(note_id: int) -> str:
    """Удалить заметку о человеке по номеру (номера видны в context)."""
    return "Удалено." if db.delete_note(note_id) else f"Заметки #{note_id} нет."


# ---------- вода ----------

@mcp.tool()
def log_water(ml: float) -> str:
    """Записать выпитую воду в миллилитрах (стакан ≈ 250, бутылка ≈ 500)."""
    db.add_water(ml)
    s = db.get_settings()
    return f"Вода: +{ml:.0f} мл, за сегодня {db.water_total():.0f} из {s.get('water_norm_ml')} мл."


# ---------- разовые напоминания ----------

def _parse_at(at: str, in_minutes: int) -> datetime | str:
    now = db.now_local()
    if in_minutes and in_minutes > 0:
        return now + timedelta(minutes=in_minutes)
    at = (at or "").strip().replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M", "%d.%m.%Y %H:%M", "%d.%m %H:%M", "%H:%M"):
        try:
            p = datetime.strptime(at, fmt)
        except ValueError:
            continue
        if fmt == "%H:%M":
            t = now.replace(hour=p.hour, minute=p.minute, second=0, microsecond=0)
            return t if t > now else t + timedelta(days=1)
        if fmt == "%d.%m %H:%M":
            p = p.replace(year=now.year)
        return p.replace(tzinfo=now.tzinfo)
    return "Не понял время. Нужно HH:MM, YYYY-MM-DD HH:MM или in_minutes."


@mcp.tool()
def remind_me(text: str, at: str = "", in_minutes: int = 0) -> str:
    """Поставить разовое напоминание: сервис сам пришлёт text в Telegram в нужный момент.
    at — «HH:MM» (сегодня, а если время прошло — завтра) или «YYYY-MM-DD HH:MM»;
    либо in_minutes — через сколько минут. Используй, когда человек просит «напомни …»."""
    when = _parse_at(at, in_minutes)
    if isinstance(when, str):
        return when
    if when <= db.now_local():
        return "Это время уже прошло."
    key = when.strftime("%Y-%m-%d %H:%M")
    db.add_custom_reminder(key, text.strip())
    return f"Напомню {when.strftime('%d.%m в %H:%M')}: {text.strip()}"


@mcp.tool()
def list_reminders() -> str:
    """Запланированные разовые напоминания."""
    rows = db.pending_reminders()
    return "\n".join(f"#{r['id']} {r['at']} — {r['text']}" for r in rows) if rows else "Разовых напоминаний нет."


@mcp.tool()
def cancel_reminder(reminder_id: int) -> str:
    """Отменить разовое напоминание по номеру."""
    return "Отменено." if db.cancel_reminder(reminder_id) else f"Напоминания #{reminder_id} нет."
