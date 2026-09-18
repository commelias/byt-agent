"""Журнал: тонкий слой над PostgreSQL (боевой) и SQLite (локальная проверка).

Все запросы идут через _run: один способ открыть соединение, один способ его переоткрыть,
если управляемая база разорвала простаивающее. Плейсхолдеры пишем в стиле «?».
"""
import logging
import sqlite3
import threading
from datetime import date, datetime, timedelta

from . import config

log = logging.getLogger("byt.db")

_IS_PG = config.DATABASE_URL.startswith(("postgresql://", "postgres://"))
_lock = threading.Lock()
_conn = None

SCHEMA = [
    """CREATE TABLE IF NOT EXISTS meals (
        id {pk}, ts TEXT NOT NULL, day TEXT NOT NULL,
        description TEXT NOT NULL, grams REAL, kcal REAL NOT NULL,
        protein REAL, fat REAL, carbs REAL)""",
    """CREATE TABLE IF NOT EXISTS workouts (
        id {pk}, ts TEXT NOT NULL, day TEXT NOT NULL,
        description TEXT NOT NULL, feeling TEXT, note TEXT)""",
    """CREATE TABLE IF NOT EXISTS calendar_marks (
        id {pk}, ts TEXT NOT NULL, day TEXT NOT NULL,
        kind TEXT NOT NULL, note TEXT)""",
    """CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY, value TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS reminders_sent (
        id {pk}, day TEXT NOT NULL, kind TEXT NOT NULL,
        UNIQUE (day, kind))""",
    """CREATE TABLE IF NOT EXISTS water (
        id {pk}, ts TEXT NOT NULL, day TEXT NOT NULL, ml REAL NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS notes (
        id {pk}, ts TEXT NOT NULL, text TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS custom_reminders (
        id {pk}, created TEXT NOT NULL, at TEXT NOT NULL, text TEXT NOT NULL,
        sent INTEGER NOT NULL DEFAULT 0)""",
    """CREATE TABLE IF NOT EXISTS delivery_log (
        id {pk}, ts TEXT NOT NULL, day TEXT NOT NULL, kind TEXT NOT NULL,
        ok INTEGER NOT NULL, detail TEXT)""",
    # очередь исходящих: всё, что сервис шлёт сам, проходит через неё
    """CREATE TABLE IF NOT EXISTS outbox (
        id {pk}, created TEXT NOT NULL, kind TEXT NOT NULL, text TEXT NOT NULL,
        sent INTEGER NOT NULL DEFAULT 0, attempts INTEGER NOT NULL DEFAULT 0,
        next_try TEXT, error TEXT)""",
    # события планировщика: и системные, и заведённые человеком — в одной таблице
    """CREATE TABLE IF NOT EXISTS events (
        id {pk}, created TEXT NOT NULL, ekey TEXT NOT NULL UNIQUE, title TEXT NOT NULL,
        at TEXT NOT NULL DEFAULT '', days TEXT NOT NULL DEFAULT 'all',
        cond TEXT NOT NULL DEFAULT '', param REAL NOT NULL DEFAULT 0,
        doc_kind TEXT NOT NULL DEFAULT 'text', doc TEXT NOT NULL DEFAULT '',
        mark TEXT NOT NULL DEFAULT '', repeat_hours REAL NOT NULL DEFAULT 0,
        max_per_day INTEGER NOT NULL DEFAULT 1, system INTEGER NOT NULL DEFAULT 0,
        enabled INTEGER NOT NULL DEFAULT 1)""",
    # длинные точные тексты (молитвенное правило и подобное) — шлются мимо модели
    """CREATE TABLE IF NOT EXISTS texts (
        key TEXT PRIMARY KEY, title TEXT NOT NULL DEFAULT '',
        body TEXT NOT NULL, updated TEXT NOT NULL)""",
    # исключения из расписания тренировок: перенос, пропуск, дополнительная
    """CREATE TABLE IF NOT EXISTS workout_exceptions (
        day TEXT PRIMARY KEY, planned INTEGER NOT NULL, note TEXT)""",
    "CREATE INDEX IF NOT EXISTS meals_day ON meals(day)",
    "CREATE INDEX IF NOT EXISTS water_day ON water(day)",
    "CREATE INDEX IF NOT EXISTS delivery_day ON delivery_log(day)",
    "CREATE INDEX IF NOT EXISTS workouts_day ON workouts(day)",
    "CREATE INDEX IF NOT EXISTS marks_day ON calendar_marks(day)",
    "CREATE INDEX IF NOT EXISTS outbox_pending ON outbox(sent, id)",
]

# Добавленные позже колонки: таблица в боевой базе уже существует и данные в ней есть.
MIGRATIONS = [("notes", "topic", "TEXT")]


def _connect():
    if _IS_PG:
        import psycopg
        from psycopg.rows import dict_row
        return psycopg.connect(config.DATABASE_URL, row_factory=dict_row, autocommit=False)
    path = config.DATABASE_URL.replace("sqlite:///", "", 1)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def _q(sql: str) -> str:
    return sql.replace("?", "%s") if _IS_PG else sql


def _drop():
    global _conn
    if _conn is not None:
        try:
            _conn.close()
        except Exception:  # noqa: BLE001
            pass
        _conn = None


def _run(sql: str, params=(), fetch: str | None = None, _retry: bool = True):
    """Выполнить запрос. fetch: None — вернуть rowcount, 'one' — строку, 'all' — список строк.
    Управляемая база рвёт простаивающее соединение молча, поэтому одна повторная попытка."""
    global _conn
    with _lock:
        try:
            if _conn is None or getattr(_conn, "closed", 0):
                _conn = _connect()
            cur = _conn.cursor()
            cur.execute(_q(sql), params)
            if fetch == "all":
                out = [dict(r) for r in cur.fetchall()]
            elif fetch == "one":
                row = cur.fetchone()
                out = dict(row) if row else None
            else:
                out = cur.rowcount
            _conn.commit()
            return out
        except Exception as e:  # noqa: BLE001
            _drop()
            if not _retry:
                log.error("Запрос не прошёл: %s: %s", type(e).__name__, e)
                raise
    return _run(sql, params, fetch, _retry=False)


def _all(sql, params=()):
    return _run(sql, params, "all")


def _one(sql, params=()):
    return _run(sql, params, "one")


def init():
    pk = "SERIAL PRIMARY KEY" if _IS_PG else "INTEGER PRIMARY KEY AUTOINCREMENT"
    for stmt in SCHEMA:
        _run(stmt.format(pk=pk))
    for table, column, decl in MIGRATIONS:
        if column not in _columns(table):
            _run(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
    for k, v in config.DEFAULT_SETTINGS.items():
        _run("INSERT INTO settings(key, value) VALUES (?, ?) ON CONFLICT (key) DO NOTHING", (k, v))
    for k in config.RETIRED_SETTINGS:
        _run("DELETE FROM settings WHERE key = ?", (k,))


def _columns(table: str) -> set[str]:
    if _IS_PG:
        rows = _all("SELECT column_name FROM information_schema.columns WHERE table_name = ?", (table,))
        return {r["column_name"] for r in rows}
    return {r["name"] for r in _all(f"PRAGMA table_info({table})")}


def now_local() -> datetime:
    return datetime.now(config.TZ)


def today() -> str:
    return now_local().date().isoformat()


def _stamp() -> str:
    return now_local().isoformat(timespec="minutes")


# ---------- питание ----------

def add_meal(description, kcal, grams=None, protein=None, fat=None, carbs=None, day=None):
    _run("INSERT INTO meals(ts, day, description, grams, kcal, protein, fat, carbs) VALUES (?,?,?,?,?,?,?,?)",
         (_stamp(), day or today(), description, grams, kcal, protein, fat, carbs))


def meals_for_day(day=None):
    return _all("SELECT * FROM meals WHERE day = ? ORDER BY id", (day or today(),))


def delete_last_meal(day=None):
    row = _one("""DELETE FROM meals WHERE id = (SELECT id FROM meals WHERE day = ? ORDER BY id DESC LIMIT 1)
                  RETURNING description""", (day or today(),))
    return row["description"] if row else None


def day_totals(day=None):
    meals = meals_for_day(day)
    t = {"kcal": 0.0, "protein": 0.0, "fat": 0.0, "carbs": 0.0, "count": len(meals)}
    for m in meals:
        for k in ("kcal", "protein", "fat", "carbs"):
            t[k] += float(m[k] or 0)
    return t


def meals_history(days: int):
    return _all("""SELECT day, COUNT(*) AS n, SUM(kcal) AS kcal, SUM(protein) AS protein,
                   SUM(fat) AS fat, SUM(carbs) AS carbs FROM meals
                   GROUP BY day ORDER BY day DESC LIMIT ?""", (days,))


# ---------- спорт ----------

def add_workout(description, feeling=None, note=None, day=None):
    _run("INSERT INTO workouts(ts, day, description, feeling, note) VALUES (?,?,?,?,?)",
         (_stamp(), day or today(), description, feeling, note))


def workouts_history(count: int):
    return _all("SELECT day, description, feeling, note FROM workouts ORDER BY id DESC LIMIT ?", (count,))


def last_workout():
    return _one("SELECT day, description FROM workouts ORDER BY id DESC LIMIT 1")


def workout_on(day: str):
    """Запись о тренировке за конкретный день, если она есть."""
    return _one("SELECT description, feeling FROM workouts WHERE day = ? ORDER BY id DESC LIMIT 1", (day,))


def set_workout_day(day: str, planned: bool, note: str = ""):
    """Исключение из расписания: перенос, пропуск, дополнительная тренировка."""
    _run("""INSERT INTO workout_exceptions(day, planned, note) VALUES (?,?,?)
            ON CONFLICT (day) DO UPDATE SET planned = EXCLUDED.planned, note = EXCLUDED.note""",
         (day, 1 if planned else 0, note))


def workout_day_override(day: str):
    return _one("SELECT planned, note FROM workout_exceptions WHERE day = ?", (day,))


DAY_CODES = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def workout_planned(day: str, workout_days: str) -> tuple[bool, str]:
    """Тренировка в этот день? Сначала исключение (перенос, пропуск), потом базовое расписание.
    Без исключений договорённость о переносе жила только в разговоре и назавтра исчезала."""
    ov = workout_day_override(day)
    if ov:
        return bool(ov["planned"]), (ov["note"] or "перенос")
    code = DAY_CODES[date.fromisoformat(day).weekday()]
    return code in workout_days.replace(" ", "").lower().split(","), ""


def workout_overrides(limit: int = 10):
    return _all("SELECT day, planned, note FROM workout_exceptions WHERE day >= ? ORDER BY day LIMIT ?",
                (today(), limit))


# ---------- календарь ----------

def add_mark(kind, note=None, day=None):
    _run("INSERT INTO calendar_marks(ts, day, kind, note) VALUES (?,?,?,?)",
         (_stamp(), day or today(), kind, note))


def marks_history(count: int):
    return _all("SELECT day, kind, note FROM calendar_marks ORDER BY id DESC LIMIT ?", (count,))


def marks_for_day(day=None):
    return _all("SELECT kind, note FROM calendar_marks WHERE day = ? ORDER BY id", (day or today(),))


# ---------- настройки ----------

def get_settings() -> dict:
    """Пользовательские настройки. Служебные ключи (с «_») сюда не попадают."""
    return {r["key"]: r["value"] for r in _all("SELECT key, value FROM settings")
            if not r["key"].startswith("_")}


def set_setting(key, value):
    _run("""INSERT INTO settings(key, value) VALUES (?, ?)
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value""", (key, value))


def get_state(key: str, default: str = "") -> str:
    row = _one("SELECT value FROM settings WHERE key = ?", ("_" + key,))
    return row["value"] if row else default


def set_state(key: str, value: str):
    set_setting("_" + key, value)


# ---------- плановые напоминания: защита от повтора ----------

def reminder_claim(day: str, kind: str) -> bool:
    """True — за этот день напоминание ещё не ставили в очередь, и мы его забираем."""
    return _run("INSERT INTO reminders_sent(day, kind) VALUES (?, ?) ON CONFLICT DO NOTHING",
                (day, kind)) == 1


# ---------- время последних записей ----------

def last_ts(table: str, day=None):
    assert table in ("meals", "water", "workouts")
    row = _one(f"SELECT ts FROM {table} WHERE day = ? ORDER BY id DESC LIMIT 1", (day or today(),))
    return datetime.fromisoformat(row["ts"]) if row else None


# ---------- вода ----------

def add_water(ml, day=None):
    _run("INSERT INTO water(ts, day, ml) VALUES (?,?,?)", (_stamp(), day or today(), ml))


def water_total(day=None) -> float:
    row = _one("SELECT COALESCE(SUM(ml), 0) AS ml FROM water WHERE day = ?", (day or today(),))
    return float(row["ml"] or 0)


# ---------- заметки о человеке ----------

def add_note(text: str, topic: str = ""):
    """Заметка с темой замещает прежнюю по той же теме: две противоречивые заметки об одном
    и том же — источник «амнезии», когда модель каждый раз выбирает любую из них."""
    topic = (topic or "").strip().lower()
    if topic and _run("UPDATE notes SET text = ?, ts = ? WHERE topic = ?", (text, _stamp(), topic)) >= 1:
        return
    _run("INSERT INTO notes(ts, text, topic) VALUES (?,?,?)", (_stamp(), text, topic or None))


def list_notes():
    return _all("SELECT id, text, topic FROM notes ORDER BY id")


def delete_note(note_id: int) -> bool:
    return _run("DELETE FROM notes WHERE id = ?", (note_id,)) == 1


# ---------- точные тексты ----------

def set_text(key: str, body: str, title: str = ""):
    _run("""INSERT INTO texts(key, title, body, updated) VALUES (?,?,?,?)
            ON CONFLICT (key) DO UPDATE SET title = EXCLUDED.title, body = EXCLUDED.body,
            updated = EXCLUDED.updated""", (key, title, body, _stamp()))


def get_text(key: str):
    return _one("SELECT key, title, body, updated FROM texts WHERE key = ?", (key,))


def list_texts():
    return _all("SELECT key, title, LENGTH(body) AS size, updated FROM texts ORDER BY key")


def delete_text(key: str) -> bool:
    return _run("DELETE FROM texts WHERE key = ?", (key,)) == 1


# ---------- события планировщика ----------

EVENT_FIELDS = ("id", "ekey", "title", "at", "days", "cond", "param",
                "doc_kind", "doc", "mark", "repeat_hours", "max_per_day", "system", "enabled")


def add_event(ekey: str, title: str, at: str = "", days: str = "all", cond: str = "",
              param: float = 0, doc_kind: str = "text", doc: str = "", mark: str = "",
              repeat_hours: float = 0, max_per_day: int = 1, system: bool = False) -> int:
    row = _one("""INSERT INTO events(created, ekey, title, at, days, cond, param, doc_kind, doc,
                  mark, repeat_hours, max_per_day, system, enabled)
                  VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,1) RETURNING id""",
               (_stamp(), ekey, title, at, days, cond, param, doc_kind, doc, mark,
                repeat_hours, max_per_day, 1 if system else 0))
    return int(row["id"])


def seed_event(**kw) -> bool:
    """Системное событие заводится один раз; потом его время и дни принадлежат человеку."""
    if _one("SELECT id FROM events WHERE ekey = ?", (kw["ekey"],)):
        return False
    add_event(**kw)
    return True


def list_events(only_enabled: bool = False):
    sql = "SELECT " + ", ".join(EVENT_FIELDS) + " FROM events"
    if only_enabled:
        sql += " WHERE enabled = 1"
    return _all(sql + " ORDER BY at, id")


def get_event(ref: str):
    """По номеру или по ключу — человеку удобнее номер, коду ключ."""
    sql = "SELECT " + ", ".join(EVENT_FIELDS) + " FROM events WHERE "
    if str(ref).isdigit():
        return _one(sql + "id = ?", (int(ref),))
    return _one(sql + "ekey = ?", (str(ref).strip().lower(),))


def update_event(eid: int, **fields) -> bool:
    allowed = {k: v for k, v in fields.items() if k in EVENT_FIELDS and k not in ("id", "ekey", "system")}
    if not allowed:
        return False
    sets = ", ".join(f"{k} = ?" for k in allowed)
    return _run(f"UPDATE events SET {sets} WHERE id = ?", (*allowed.values(), eid)) >= 1


def delete_event(eid: int) -> bool:
    return _run("DELETE FROM events WHERE id = ? AND system = 0", (eid,)) == 1


# ---------- разовые напоминания ----------

def add_custom_reminder(at: str, text: str):
    _run("INSERT INTO custom_reminders(created, at, text, sent) VALUES (?,?,?,0)", (_stamp(), at, text))


def pending_reminders():
    return _all("SELECT id, at, text FROM custom_reminders WHERE sent = 0 ORDER BY at")


def mark_reminder_sent(rid: int):
    _run("UPDATE custom_reminders SET sent = 1 WHERE id = ?", (rid,))


def cancel_reminder(rid: int) -> bool:
    return _run("DELETE FROM custom_reminders WHERE id = ? AND sent = 0", (rid,)) == 1


# ---------- очередь исходящих ----------

def enqueue(kind: str, text: str):
    """Положить сообщение в очередь. Отправкой и повторами занимается воркер планировщика."""
    _run("INSERT INTO outbox(created, kind, text, sent, attempts) VALUES (?,?,?,0,0)",
         (_stamp(), kind, text))


def outbox_pending(limit: int = 10):
    return _all("""SELECT id, kind, text, attempts FROM outbox
                   WHERE sent = 0 AND (next_try IS NULL OR next_try <= ?)
                   ORDER BY id LIMIT ?""", (_stamp(), limit))


def outbox_done(oid: int):
    _run("UPDATE outbox SET sent = 1, error = NULL WHERE id = ?", (oid,))


def outbox_retry(oid: int, attempts: int, next_try: str, error: str):
    _run("UPDATE outbox SET attempts = ?, next_try = ?, error = ? WHERE id = ?",
         (attempts, next_try, error[:300], oid))


def outbox_give_up(oid: int, attempts: int, error: str):
    _run("UPDATE outbox SET sent = 2, attempts = ?, error = ? WHERE id = ?", (attempts, error[:300], oid))


def outbox_undelivered(hours: int = 12, limit: int = 5):
    """Что сервис хотел прислать, но Telegram не пропустил (ждёт повтора или брошено).
    Старше окна — не вытаскиваем: вчерашнее «поешь» уже ни к чему."""
    since = (now_local() - timedelta(hours=hours)).isoformat(timespec="minutes")
    return _all("""SELECT id, created, kind, text FROM outbox
                   WHERE sent IN (0, 2) AND created >= ? ORDER BY id LIMIT ?""", (since, limit))


def outbox_handed(ids: list[int]):
    """Передано человеку через ответ агента: больше не шлём и не показываем (sent = 3)."""
    for oid in ids:
        _run("UPDATE outbox SET sent = 3, error = NULL WHERE id = ? AND sent IN (0, 2)", (oid,))


def outbox_stuck(limit: int = 10):
    return _all("SELECT id, created, kind, attempts, error FROM outbox WHERE sent = 2 ORDER BY id DESC LIMIT ?",
                (limit,))


# ---------- журнал доставки ----------

def log_delivery(kind: str, ok: bool, detail: str = ""):
    now = now_local()
    _run("INSERT INTO delivery_log(ts, day, kind, ok, detail) VALUES (?,?,?,?,?)",
         (now.isoformat(timespec="minutes"), now.date().isoformat(), kind, 1 if ok else 0, (detail or "")[:300]))


def deliveries(day=None, limit: int = 30):
    if day:
        return _all("SELECT ts, kind, ok, detail FROM delivery_log WHERE day = ? ORDER BY id", (day,))
    return _all("SELECT ts, kind, ok, detail FROM delivery_log ORDER BY id DESC LIMIT ?", (limit,))


def count_deliveries(day: str, kind: str) -> int:
    row = _one("SELECT COUNT(*) AS n FROM delivery_log WHERE day = ? AND kind = ? AND ok = 1", (day, kind))
    return int(row["n"])
