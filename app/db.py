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
    "CREATE INDEX IF NOT EXISTS meals_day ON meals(day)",
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
    with cursor() as cur:
        cur.execute("SELECT key, value FROM settings")
        return {r["key"]: r["value"] for r in cur.fetchall()}


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
