"""Журнал: единый тонкий слой над PostgreSQL (боевой) и SQLite (локальная проверка)."""
import sqlite3
import threading
from contextlib import contextmanager
from datetime import date, datetime

from . import config

_IS_PG = config.DATABASE_URL.startswith(("postgresql://", "postgres://"))
_lock = threading.Lock()

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
    "CREATE INDEX IF NOT EXISTS meals_day ON meals(day)",
    "CREATE INDEX IF NOT EXISTS water_day ON water(day)",
    "CREATE INDEX IF NOT EXISTS delivery_day ON delivery_log(day)",
    "CREATE INDEX IF NOT EXISTS workouts_day ON workouts(day)",
    "CREATE INDEX IF NOT EXISTS marks_day ON calendar_marks(day)",
]


def _connect():
    if _IS_PG:
        import psycopg
        from psycopg.rows import dict_row
        return psycopg.connect(config.DATABASE_URL, row_factory=dict_row)
    path = config.DATABASE_URL.replace("sqlite:///", "", 1)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def _q(sql: str) -> str:
    """Плейсхолдеры: пишем в стиле '?', для PostgreSQL меняем на '%s'."""
    return sql.replace("?", "%s") if _IS_PG else sql


@contextmanager
def cursor():
    with _lock:
        conn = _connect()
        try:
            cur = conn.cursor()
            yield cur
            conn.commit()
        finally:
            conn.close()


def init():
    pk = "SERIAL PRIMARY KEY" if _IS_PG else "INTEGER PRIMARY KEY AUTOINCREMENT"
    with cursor() as cur:
        for stmt in SCHEMA:
            cur.execute(stmt.format(pk=pk))
        for k, v in config.DEFAULT_SETTINGS.items():
            if _IS_PG:
                cur.execute("INSERT INTO settings(key, value) VALUES (%s, %s) ON CONFLICT (key) DO NOTHING", (k, v))
            else:
                cur.execute("INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)", (k, v))


def now_local() -> datetime:
    return datetime.now(config.TZ)


def today() -> str:
    return now_local().date().isoformat()


def _rows(cur):
    return [dict(r) for r in cur.fetchall()]


# ---------- питание ----------

def add_meal(description, kcal, grams=None, protein=None, fat=None, carbs=None, day=None):
    day = day or today()
    with cursor() as cur:
        cur.execute(_q("INSERT INTO meals(ts, day, description, grams, kcal, protein, fat, carbs) VALUES (?,?,?,?,?,?,?,?)"),
                    (now_local().isoformat(timespec="minutes"), day, description, grams, kcal, protein, fat, carbs))


def meals_for_day(day=None):
    day = day or today()
    with cursor() as cur:
        cur.execute(_q("SELECT * FROM meals WHERE day = ? ORDER BY id"), (day,))
        return _rows(cur)


def delete_last_meal(day=None):
    day = day or today()
    with cursor() as cur:
        cur.execute(_q("SELECT id, description FROM meals WHERE day = ? ORDER BY id DESC LIMIT 1"), (day,))
        row = cur.fetchone()
        if not row:
            return None
        cur.execute(_q("DELETE FROM meals WHERE id = ?"), (row["id"],))
        return row["description"]


def day_totals(day=None):
    meals = meals_for_day(day)
    t = {"kcal": 0.0, "protein": 0.0, "fat": 0.0, "carbs": 0.0, "count": len(meals)}
    for m in meals:
        for k in ("kcal", "protein", "fat", "carbs"):
            t[k] += float(m[k] or 0)
    return t


def meals_history(days: int):
    with cursor() as cur:
        cur.execute(_q("""SELECT day, COUNT(*) AS n, SUM(kcal) AS kcal, SUM(protein) AS protein,
                          SUM(fat) AS fat, SUM(carbs) AS carbs FROM meals
                          GROUP BY day ORDER BY day DESC LIMIT ?"""), (days,))
        return _rows(cur)


# ---------- спорт ----------

def add_workout(description, feeling=None, note=None, day=None):
    day = day or today()
    with cursor() as cur:
        cur.execute(_q("INSERT INTO workouts(ts, day, description, feeling, note) VALUES (?,?,?,?,?)"),
                    (now_local().isoformat(timespec="minutes"), day, description, feeling, note))


def workouts_history(days: int):
    with cursor() as cur:
        cur.execute(_q("SELECT day, description, feeling, note FROM workouts ORDER BY id DESC LIMIT ?"), (days,))
        return _rows(cur)


# ---------- календарь ----------

def add_mark(kind, note=None, day=None):
    day = day or today()
    with cursor() as cur:
        cur.execute(_q("INSERT INTO calendar_marks(ts, day, kind, note) VALUES (?,?,?,?)"),
                    (now_local().isoformat(timespec="minutes"), day, kind, note))


def marks_history(days: int):
    with cursor() as cur:
        cur.execute(_q("SELECT day, kind, note FROM calendar_marks ORDER BY id DESC LIMIT ?"), (days,))
        return _rows(cur)


# ---------- настройки ----------

def get_settings() -> dict:
    """Пользовательские настройки. Служебные ключи (с «_») сюда не попадают."""
    with cursor() as cur:
        cur.execute("SELECT key, value FROM settings")
        return {r["key"]: r["value"] for r in cur.fetchall() if not r["key"].startswith("_")}


def get_state(key: str, default: str = "") -> str:
    with cursor() as cur:
        cur.execute(_q("SELECT value FROM settings WHERE key = ?"), ("_" + key,))
        row = cur.fetchone()
        return row["value"] if row else default


def set_state(key: str, value: str):
    set_setting("_" + key, value)


def set_setting(key, value):
    with cursor() as cur:
        if _IS_PG:
            cur.execute("INSERT INTO settings(key, value) VALUES (%s, %s) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", (key, value))
        else:
            cur.execute("INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)", (key, value))


# ---------- напоминания: защита от повтора ----------

def reminder_claim(day: str, kind: str) -> bool:
    """True — напоминание за этот день ещё не слали, и мы его забираем."""
    with cursor() as cur:
        if _IS_PG:
            cur.execute("INSERT INTO reminders_sent(day, kind) VALUES (%s, %s) ON CONFLICT DO NOTHING", (day, kind))
        else:
            cur.execute("INSERT OR IGNORE INTO reminders_sent(day, kind) VALUES (?, ?)", (day, kind))
        return cur.rowcount == 1


def reminder_unclaim(day: str, kind: str):
    """Отправка не удалась — разрешаем следующему тику попробовать снова."""
    with cursor() as cur:
        cur.execute(_q("DELETE FROM reminders_sent WHERE day = ? AND kind = ?"), (day, kind))


# ---------- время последних записей ----------

def last_ts(table: str, day=None):
    """Время последней записи за день в meals / water / workouts, или None."""
    assert table in ("meals", "water", "workouts")
    day = day or today()
    with cursor() as cur:
        cur.execute(_q(f"SELECT ts FROM {table} WHERE day = ? ORDER BY id DESC LIMIT 1"), (day,))
        row = cur.fetchone()
        return datetime.fromisoformat(row["ts"]) if row else None


def last_workout():
    with cursor() as cur:
        cur.execute("SELECT day, description FROM workouts ORDER BY id DESC LIMIT 1")
        row = cur.fetchone()
        return dict(row) if row else None


# ---------- вода ----------

def add_water(ml, day=None):
    day = day or today()
    with cursor() as cur:
        cur.execute(_q("INSERT INTO water(ts, day, ml) VALUES (?,?,?)"),
                    (now_local().isoformat(timespec="minutes"), day, ml))


def water_total(day=None) -> float:
    day = day or today()
    with cursor() as cur:
        cur.execute(_q("SELECT COALESCE(SUM(ml), 0) AS ml FROM water WHERE day = ?"), (day,))
        return float(cur.fetchone()["ml"] or 0)


# ---------- заметки о человеке (память о просьбах и привычках) ----------

def add_note(text):
    with cursor() as cur:
        cur.execute(_q("INSERT INTO notes(ts, text) VALUES (?,?)"),
                    (now_local().isoformat(timespec="minutes"), text))


def list_notes():
    with cursor() as cur:
        cur.execute("SELECT id, text FROM notes ORDER BY id")
        return _rows(cur)


def delete_note(note_id: int) -> bool:
    with cursor() as cur:
        cur.execute(_q("DELETE FROM notes WHERE id = ?"), (note_id,))
        return cur.rowcount == 1


# ---------- разовые напоминания ----------

def add_custom_reminder(at: str, text: str):
    """at — 'YYYY-MM-DD HH:MM' по местному времени."""
    with cursor() as cur:
        cur.execute(_q("INSERT INTO custom_reminders(created, at, text, sent) VALUES (?,?,?,0)"),
                    (now_local().isoformat(timespec="minutes"), at, text))


def pending_reminders():
    with cursor() as cur:
        cur.execute("SELECT id, at, text FROM custom_reminders WHERE sent = 0 ORDER BY at")
        return _rows(cur)


def mark_reminder_sent(rid: int):
    with cursor() as cur:
        cur.execute(_q("UPDATE custom_reminders SET sent = 1 WHERE id = ?"), (rid,))


def cancel_reminder(rid: int) -> bool:
    with cursor() as cur:
        cur.execute(_q("DELETE FROM custom_reminders WHERE id = ? AND sent = 0"), (rid,))
        return cur.rowcount == 1


# ---------- журнал доставки ----------

def log_delivery(kind: str, ok: bool, detail: str = ""):
    now = now_local()
    with cursor() as cur:
        cur.execute(_q("INSERT INTO delivery_log(ts, day, kind, ok, detail) VALUES (?,?,?,?,?)"),
                    (now.isoformat(timespec="minutes"), now.date().isoformat(), kind, 1 if ok else 0, detail[:300]))


def deliveries(day=None, limit: int = 30):
    with cursor() as cur:
        if day:
            cur.execute(_q("SELECT ts, kind, ok, detail FROM delivery_log WHERE day = ? ORDER BY id"), (day,))
        else:
            cur.execute(_q("SELECT ts, kind, ok, detail FROM delivery_log ORDER BY id DESC LIMIT ?"), (limit,))
        return _rows(cur)


def count_deliveries(day: str, kind: str) -> int:
    with cursor() as cur:
        cur.execute(_q("SELECT COUNT(*) AS n FROM delivery_log WHERE day = ? AND kind = ? AND ok = 1"), (day, kind))
        return int(cur.fetchone()["n"])
