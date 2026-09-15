"""MCP-сервер «Журнал» — руки агента: записать, прочитать, настроить, напомнить."""
import re
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
        "заметки о человеке, напоминания и точные тексты. Перед ответом на каждое сообщение вызывай "
        "context — это текущее время и память. Записывай каждый приём пищи, воду и тренировку сразу."
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


def _day(value: str) -> str:
    """Пустая строка — сегодня; «завтра» и «вчера» тоже понимаем, чтобы не гадать с датами."""
    v = (value or "").strip().lower()
    today = db.now_local().date()
    if not v or v == "сегодня":
        return today.isoformat()
    if v == "завтра":
        return (today + timedelta(days=1)).isoformat()
    if v == "вчера":
        return (today - timedelta(days=1)).isoformat()
    try:
        return date.fromisoformat(v).isoformat()
    except ValueError:
        return ""


# ---------- питание ----------

@mcp.tool()
def log_meal(description: str, kcal: float, grams: float = 0,
             protein: float = 0, fat: float = 0, carbs: float = 0,
             day: str = "") -> str:
    """Записать еду. description — блюдо, kcal — калории, grams — вес, protein/fat/carbs — БЖУ,
    day — YYYY-MM-DD (по умолчанию сегодня). Возвращает итог дня."""
    db.add_meal(description, kcal, grams, protein, fat, carbs, _day(day) or None)
    return f"Записано: {description} — {kcal:.0f} ккал.\n" + _totals_line(db.day_totals(_day(day)), db.get_settings())


@mcp.tool()
def day_summary(day: str = "") -> str:
    """Итог питания за день: что съедено, суммы против норм."""
    d = _day(day)
    meals = db.meals_for_day(d)
    if not meals:
        return f"За {d} записей о еде нет."
    lines = [f"{m['ts'][11:16]} {m['description']} — {_fmt(m['grams'])} г, {_fmt(m['kcal'])} ккал, "
             f"Б {_fmt(m['protein'])} / Ж {_fmt(m['fat'])} / У {_fmt(m['carbs'])}" for m in meals]
    return "\n".join(lines) + "\n" + _totals_line(db.day_totals(d), db.get_settings())


@mcp.tool()
def undo_last_meal(day: str = "") -> str:
    """Удалить последнюю запись о еде за день."""
    removed = db.delete_last_meal(_day(day))
    return f"Удалено: {removed}" if removed else "Удалять нечего — записей за день нет."


@mcp.tool()
def meals_history(days: int = 7) -> str:
    """Питание по дням за последние N дней."""
    rows = db.meals_history(days)
    if not rows:
        return "История пуста."
    return "\n".join(f"{r['day']}: {float(r['kcal'] or 0):.0f} ккал, Б {float(r['protein'] or 0):.0f} / "
                     f"Ж {float(r['fat'] or 0):.0f} / У {float(r['carbs'] or 0):.0f} ({r['n']} записей)" for r in rows)


@mcp.tool()
def log_water(ml: float) -> str:
    """Записать воду в мл (стакан 250, бутылка 500)."""
    db.add_water(ml)
    s = db.get_settings()
    return f"Вода: +{ml:.0f} мл, за сегодня {db.water_total():.0f} из {s.get('water_norm_ml')} мл."


# ---------- спорт ----------

@mcp.tool()
def log_workout(description: str, feeling: str = "", note: str = "", day: str = "") -> str:
    """Записать тренировку: что сделано, самочувствие, замечания (боль, пропуск)."""
    db.add_workout(description, feeling, note, _day(day) or None)
    return "Тренировка записана."


@mcp.tool()
def workouts_history(count: int = 10) -> str:
    """Последние тренировки."""
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
    """Программа, дни тренировок и ближайшие переносы."""
    s = db.get_settings()
    out = [f"Дни: {s.get('workout_days')}, напоминание в {s.get('workout_time')}, "
           f"вечерняя проверка в {s.get('workout_check_time')}."]
    ex = db.workout_overrides()
    if ex:
        out.append("Исключения: " + "; ".join(
            f"{e['day']} — {'тренировка' if e['planned'] else 'без тренировки'}"
            + (f" ({e['note']})" if e["note"] else "") for e in ex))
    out.append("Программа:\n" + (s.get("workout_program") or "(не задана — спроси и сохрани через set_setting workout_program)"))
    return "\n".join(out)


@mcp.tool()
def move_workout(to_day: str, from_day: str = "", note: str = "") -> str:
    """Перенести тренировку: to_day — когда будет, from_day — откуда сняли (можно пусто).
    Даты YYYY-MM-DD или «сегодня»/«завтра». Вечером сервис сам спросит про неё в новый день.
    Обязательно вызывай, когда договорились о переносе, иначе расписание останется прежним."""
    to_iso = _day(to_day)
    if not to_iso:
        return "Не понял дату переноса. Нужно YYYY-MM-DD, «сегодня» или «завтра»."
    src = _day(from_day) if from_day else ""
    db.set_workout_day(to_iso, True, note.strip() or (f"перенос с {src}" if src else "перенос"))
    if src:
        db.set_workout_day(src, False, f"перенесена на {to_iso}")
    return f"Тренировка перенесена{f' с {src}' if src else ''} на {to_iso}. Вечером спрошу о ней."


# ---------- календарь и духовная часть ----------

@mcp.tool()
def today_calendar(day: str = "") -> str:
    """Православный календарь на день и завтра: пост, причина, праздник. Даты не помни сама — бери здесь."""
    d = date.fromisoformat(_day(day))
    return "Сегодня — " + orthodox.human(d) + "\nЗавтра — " + orthodox.human(d + timedelta(days=1))


@mcp.tool()
def log_calendar_mark(kind: str, note: str = "", day: str = "") -> str:
    """Отметить: kind — пост_соблюдён / пост_нарушен / график_соблюдён / график_нарушен /
    правило_прочитано / правило_пропущено. note — слова человека."""
    db.add_mark(kind, note, _day(day) or None)
    return "Отмечено."


@mcp.tool()
def calendar_marks_history(count: int = 14) -> str:
    """Последние отметки по календарю и правилу."""
    rows = db.marks_history(count)
    if not rows:
        return "Отметок нет."
    return "\n".join(f"{r['day']}: {r['kind']}" + (f" — {r['note']}" if r["note"] else "") for r in rows)


# ---------- точные тексты ----------

@mcp.tool()
def saved_text(key: str, body: str = "", title: str = "") -> str:
    """Точный текст, который сервис шлёт человеку дословно, минуя тебя: молитвенное правило,
    программа, список. Без body — сведения о сохранённом; с body — сохранить или заменить.
    Сам текст тебе не возвращается: длинные тексты пересказывать нельзя, только отправлять."""
    key = key.strip().lower()
    if body:
        db.set_text(key, body.strip(), title.strip())
        return f"Текст «{key}» сохранён, {len(body.strip())} знаков. Отправить — send_saved_text."
    saved = db.get_text(key)
    if not saved:
        have = ", ".join(t["key"] for t in db.list_texts()) or "ничего"
        return f"Текста «{key}» нет. Сохранено: {have}."
    return (f"«{key}»: {saved['title'] or 'без заголовка'}, {len(saved['body'])} знаков, "
            f"обновлён {saved['updated']}. Первые строки: {saved['body'][:120]}…")


@mcp.tool()
def send_saved_text(key: str) -> str:
    """Отправить человеку сохранённый текст дословно. Пользуйся этим вместо того, чтобы
    выписывать длинный текст в ответе: так он не оборвётся и не изменится."""
    key = key.strip().lower()
    saved = db.get_text(key)
    if not saved:
        return f"Текста «{key}» нет."
    title = (saved["title"] or "").strip()
    db.enqueue("сохранённый текст", (f"{title}\n\n" if title else "") + saved["body"])
    return "Отправила."


# ---------- события планировщика ----------

MARKS = ("тренировка", "еда", "вода")


@mcp.tool()
def list_events() -> str:
    """Все события планировщика: что и когда сервис присылает сам. Системные не удаляются,
    но им можно менять время и дни и выключать их."""
    rows = db.list_events()
    if not rows:
        return "Событий нет."
    out = []
    for r in rows:
        when = r["at"] or (f"каждые {r['param']:.0f} ч" if r["repeat_hours"] else "—")
        line = f"#{r['id']} {when} {r['days'] or 'all'} — {r['title']}"
        if r["doc_kind"] == "saved":
            line += f" [текст: {r['doc']}]"
        if r["cond"]:
            line += f" [если: {r['cond']}]"
        if not r["enabled"]:
            line += " (выключено)"
        if r["system"]:
            line += " ·сист"
        out.append(line)
    return "\n".join(out)


@mcp.tool()
def add_event(title: str, at: str, days: str = "all", text: str = "",
              text_key: str = "", mark: str = "") -> str:
    """Завести повторяющееся событие: title — название, at — время HH:MM, days — all или mon,wed,fri.
    Что прислать: text (короткий текст) либо text_key (ключ сохранённого точного текста).
    mark — что потом отметить в журнале, например «правило». Просит напоминать регулярно —
    только так: заметка ничего не пришлёт, присылает сервис."""
    if not _hm_ok(at):
        return "Время нужно в виде HH:MM."
    if not text and not text_key:
        return "Нужен text или text_key."
    if text_key and not db.get_text(text_key.strip().lower()):
        return f"Текста «{text_key}» нет — сначала сохрани его через saved_text."
    ekey = "u" + db.now_local().strftime("%m%d%H%M%S")
    eid = db.add_event(ekey=ekey, title=title.strip(), at=at.strip(), days=days.strip().lower(),
                       doc_kind="saved" if text_key else "text",
                       doc=(text_key.strip().lower() if text_key else text.strip()),
                       mark=mark.strip().lower())
    now = db.now_local()
    first = _first_run(now, at.strip(), days.strip().lower())
    if first.date() == now.date():
        when = "сегодня"
    else:
        # Время на сегодня уже прошло: забираем сегодняшний запуск, чтобы планировщик
        # не прислал событие через минуту «вдогонку» в окне догона.
        db.reminder_claim(now.date().isoformat(), "ev:" + ekey)
        when = "завтра" if first.date() == now.date() + timedelta(days=1) else first.strftime("%d.%m")
    return (f"Событие #{eid} «{title.strip()}» в {at.strip()}, дни: {days.strip().lower()}. "
            f"Первый раз придёт {when} в {first.strftime('%H:%M')}.")


@mcp.tool()
def edit_event(event: str, at: str = "", days: str = "", on: str = "", text: str = "") -> str:
    """Поменять событие по номеру: at — новое время HH:MM, days — all или mon,wed,fri,
    on — «да»/«нет» (включить или выключить), text — новый текст. Так же меняется
    и время системных напоминаний."""
    row = db.get_event(event)
    if not row:
        return f"События {event} нет."
    fields = {}
    if at:
        if not _hm_ok(at):
            return "Время нужно в виде HH:MM."
        fields["at"] = at.strip()
    if days:
        fields["days"] = days.strip().lower()
    if on:
        fields["enabled"] = 1 if on.strip().lower() in ("да", "вкл", "yes", "on", "1") else 0
    if text:
        if row["doc_kind"] != "text":
            return "У этого события документ не текстовый — меняй его через saved_text."
        fields["doc"] = text.strip()
    if not fields:
        return "Нечего менять."
    db.update_event(row["id"], **fields)
    r = db.get_event(row["id"])
    return (f"#{r['id']} «{r['title']}»: {r['at'] or 'по условию'}, дни {r['days'] or 'all'}"
            + ("" if r["enabled"] else ", выключено"))


@mcp.tool()
def remove_event(event: str) -> str:
    """Убрать событие по номеру. Системные не удаляются — их можно только выключить через edit_event."""
    row = db.get_event(event)
    if not row:
        return f"События {event} нет."
    if row["system"]:
        return f"«{row['title']}» — системное событие, его можно только выключить: edit_event с on=нет."
    return "Убрано." if db.delete_event(row["id"]) else "Не получилось убрать."


# ---------- разовые напоминания ----------

_TIME_RE = re.compile(r"\b\d{1,2}:\d{2}\b")
_REMIND_WORDS = ("напомина", "напомни", "каждое утро", "каждый вечер", "каждый день",
                 "по утрам", "по вечерам", "ежедневно")
_DAY_KEYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def _looks_like_reminder(text: str) -> bool:
    """Заметка с временем или словом «напоминай» — это событие, а не память: заметка не присылает."""
    t = (text or "").lower()
    return bool(_TIME_RE.search(t)) or any(w in t for w in _REMIND_WORDS)


def _first_run(now: datetime, hhmm: str, days: str) -> datetime:
    """Когда событие сработает впервые: сегодня, если время не прошло и день подходит, иначе ближайший день."""
    h, m = map(int, hhmm.split(":"))
    allowed = None
    if days and days != "all":
        allowed = {_DAY_KEYS.index(d.strip()) for d in days.split(",") if d.strip() in _DAY_KEYS}
    for shift in range(8):
        cand = (now + timedelta(days=shift)).replace(hour=h, minute=m, second=0, microsecond=0)
        if allowed is not None and cand.weekday() not in allowed:
            continue
        if cand > now:
            return cand
    return now


def _hm_ok(hhmm: str) -> bool:
    try:
        h, m = map(int, hhmm.strip().split(":"))
        return 0 <= h < 24 and 0 <= m < 60
    except (ValueError, AttributeError):
        return False


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
    """Разовое напоминание: сервис пришлёт text в Telegram. at — «HH:MM» (сегодня, прошло — завтра)
    или «YYYY-MM-DD HH:MM»; либо in_minutes. Регулярное — не сюда, а в add_event."""
    when = _parse_at(at, in_minutes)
    if isinstance(when, str):
        return when
    if when <= db.now_local():
        return "Это время уже прошло."
    db.add_custom_reminder(when.strftime("%Y-%m-%d %H:%M"), text.strip())
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


# ---------- настройки ----------

@mcp.tool()
def get_settings() -> str:
    """Все настройки со значениями."""
    s = db.get_settings()
    return "\n".join(f"{k} = {v}" for k, v in sorted(s.items()))


@mcp.tool()
def set_setting(key: str, value: str) -> str:
    """Изменить настройку. Ключи: norm_kcal, norm_protein, norm_fat, norm_carbs, water_norm_ml,
    workout_days (mon..sun), workout_program, abstinence_text, wake_time, sleep_time.
    Время напоминаний здесь не живёт — оно в событиях, меняй через edit_event."""
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


def _pending_marks(day: str) -> list[str]:
    """О чём сервис сегодня спросил и ответа ещё нет. Без этого вопрос уходит в пустоту:
    бот спросил, человек ответил, а записывать оказалось нечем."""
    done = {"тренировка": bool(db.workout_on(day)),
            "еда": db.day_totals(day)["count"] > 0,
            "вода": db.water_total(day) > 0}
    kinds = {m["kind"] for m in db.marks_for_day(day)}
    out = []
    for ev in db.list_events(only_enabled=True):
        mark = (ev["mark"] or "").strip()
        if not mark or db.get_state("ждёт:" + mark) != day:
            continue
        if done.get(mark, any(k.startswith(mark) for k in kinds)):
            continue
        if mark not in out:
            out.append(mark)
    return out


@mcp.tool()
def context() -> str:
    """ВЫЗЫВАЙ ПЕРЕД КАЖДЫМ ОТВЕТОМ: время, календарь, еда и вода за сегодня, тренировка,
    заметки о человеке, напоминания, что сервис уже прислал."""
    now = db.now_local()
    s = db.get_settings()
    day = now.date()
    iso = day.isoformat()
    out = [f"{WEEKDAYS[now.weekday()]} {now.strftime('%d.%m.%Y %H:%M')}, {_part_of_day(now.hour)}; "
           f"подъём {s.get('wake_time')}, отбой {s.get('sleep_time')}",
           "Сегодня: " + orthodox.human(day),
           "Завтра: " + orthodox.human(day + timedelta(days=1))]

    t = db.day_totals()
    last_meal = db.last_ts("meals")
    out.append(f"Еда: {t['kcal']:.0f}/{s.get('norm_kcal')} ккал, {t['count']} записей"
               + (f", последняя {last_meal.strftime('%H:%M')}" if last_meal else ""))
    last_water = db.last_ts("water")
    out.append(f"Вода: {db.water_total():.0f}/{s.get('water_norm_ml')} мл"
               + (f", последняя {last_water.strftime('%H:%M')}" if last_water else ""))

    planned, why = db.workout_planned(iso, s.get("workout_days", ""))
    lw = db.last_workout()
    out.append(f"Тренировка сегодня по плану: {'да' if planned else 'нет'}"
               + (f" ({why})" if why else "")
               + (", записана" if db.workout_on(iso) else "")
               + "; последняя: " + (f"{lw['day']} — {lw['description'][:60]}" if lw else "нет"))

    marks = db.marks_for_day(iso)
    if marks:
        out.append("Отметки сегодня: " + ", ".join(m["kind"] for m in marks))

    notes = db.list_notes()
    if notes:
        out.append("Заметки (соблюдай): " + "; ".join(
            f"#{n['id']} " + (f"[{n['topic']}] " if n.get("topic") else "") + n["text"] for n in notes))

    evs = [e for e in db.list_events(only_enabled=True) if e["at"]]
    if notes:
        times = {e["at"] for e in evs}
        orphans = [f"#{n['id']}" for n in notes
                   if _TIME_RE.search(n["text"]) and not any(t in n["text"] for t in times)]
        if orphans:
            out.append("ВНИМАНИЕ: заметки " + ", ".join(orphans)
                       + " обещают напоминание, а события нет — заведи add_event и удали заметку")
    if evs:
        out.append("Сервис присылает сам: " + "; ".join(f"#{e['id']} {e['at']} {e['title']}" for e in evs))
    waiting = _pending_marks(iso)
    if waiting:
        out.append("Спросил и ждёт ответа: " + ", ".join(waiting) + " — получишь ответ, сразу запиши")
    rem = db.pending_reminders()
    if rem:
        out.append("Напоминания: " + "; ".join(f"#{r['id']} {r['at']} {r['text']}" for r in rem))
    sent = [d for d in db.deliveries(iso) if d["ok"]]
    if sent:
        out.append("Сервис прислал сам: " + ", ".join(f"{d['ts'][11:16]} {d['kind']}" for d in sent))
    return "\n".join(out)


@mcp.tool()
def remember(note: str, topic: str = "") -> str:
    """Запомнить надолго просьбу или привычку. topic — короткий ключ темы (молитва, еда, сон):
    новая заметка по той же теме заменяет прежнюю, иначе противоречия копятся и ты забываешь.
    Указывай topic всегда. Тексты с временем ЧЧ:ММ или словом «напоминай» отвергаются — это add_event."""
    if not (topic or "").strip().lower().startswith("факт") and _looks_like_reminder(note):
        return ("Это напоминание, а не заметка — заметка ничего не пришлёт. Регулярно — "
                "add_event(title, at, text или text_key), разово — remind_me. Заведи событие "
                "и назови человеку время первого срабатывания.")
    db.add_note(note.strip(), topic)
    return f"Запомнено ({topic.strip().lower() or 'без темы'}): {note.strip()}"


@mcp.tool()
def forget(note_id: int) -> str:
    """Удалить заметку по номеру из context."""
    return "Удалено." if db.delete_note(note_id) else f"Заметки #{note_id} нет."
