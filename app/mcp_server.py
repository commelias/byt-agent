"""MCP-сервер «Журнал» — руки агента: записать, прочитать, настроить."""
from datetime import date, timedelta

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from . import config, db, orthodox

mcp = FastMCP(
    "byt-journal",
transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    instructions=(
        "Журнал быта одного человека: питание, тренировки, православный календарь, настройки. "
        "Записывай каждый приём пищи и тренировку сразу, как только посчитал. "
        "Даты постов и праздников бери из today_calendar, а не из памяти."
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
    workout_program (текст), calendar_time, abstinence_days, abstinence_time, abstinence_text.
    Пустая строка в abstinence_days выключает личный график."""
    if key not in config.DEFAULT_SETTINGS:
        return f"Неизвестный ключ {key}. Допустимые: {', '.join(config.DEFAULT_SETTINGS)}"
    db.set_setting(key, value.strip())
    return f"{key} = {value.strip()}"
