"""Православный календарь, посчитанный формулой, а не памятью модели.

Пасха — по алгоритму Гаусса для юлианского календаря, затем перевод в григорианский
(+13 дней, верно для 1900–2099). Все даты ниже — по новому стилю.
"""
from datetime import date, timedelta

RU_WEEKDAYS = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]


def pascha(year: int) -> date:
    a = year % 4
    b = year % 7
    c = year % 19
    d = (19 * c + 15) % 30
    e = (2 * a + 4 * b - d + 34) % 7
    month = (d + e + 114) // 31
    day = (d + e + 114) % 31 + 1
    julian = date(year, month, day)
    return julian + timedelta(days=13)


def _in(d: date, start: date, end: date) -> bool:
    return start <= d <= end


def feast(d: date) -> str | None:
    """Пасха и двунадесятые праздники."""
    p = pascha(d.year)
    moving = {
        p: "Пасха",
        p - timedelta(days=7): "Вход Господень в Иерусалим (Вербное воскресенье)",
        p + timedelta(days=39): "Вознесение Господне",
        p + timedelta(days=49): "День Святой Троицы (Пятидесятница)",
    }
    if d in moving:
        return moving[d]
    fixed = {
        (1, 7): "Рождество Христово",
        (1, 19): "Крещение Господне (Богоявление)",
        (2, 15): "Сретение Господне",
        (4, 7): "Благовещение Пресвятой Богородицы",
        (8, 19): "Преображение Господне",
        (8, 28): "Успение Пресвятой Богородицы",
        (9, 21): "Рождество Пресвятой Богородицы",
        (9, 27): "Воздвижение Креста Господня",
        (12, 4): "Введение во храм Пресвятой Богородицы",
    }
    return fixed.get((d.month, d.day))


def multi_day_fast(d: date) -> str | None:
    p = pascha(d.year)
    if _in(d, p - timedelta(days=48), p - timedelta(days=1)):
        return "Великий пост"
    if _in(d, p + timedelta(days=57), date(d.year, 7, 11)):
        return "Петров пост"
    if _in(d, date(d.year, 8, 14), date(d.year, 8, 27)):
        return "Успенский пост"
    if d >= date(d.year, 11, 28) or d <= date(d.year, 1, 6):
        return "Рождественский пост"
    return None


def fast_free_week(d: date) -> str | None:
    """Сплошные седмицы — среда и пятница не постные."""
    p = pascha(d.year)
    if _in(d, date(d.year, 1, 7), date(d.year, 1, 17)):
        return "Святки"
    if _in(d, p - timedelta(days=69), p - timedelta(days=63)):
        return "Седмица мытаря и фарисея"
    if _in(d, p - timedelta(days=55), p - timedelta(days=49)):
        return "Сырная седмица (Масленица): без мяса, но без поста в среду и пятницу"
    if _in(d, p + timedelta(days=1), p + timedelta(days=6)):
        return "Светлая седмица"
    if _in(d, p + timedelta(days=50), p + timedelta(days=56)):
        return "Троицкая седмица"
    return None


def strict_one_day_fast(d: date) -> str | None:
    fixed = {
        (1, 18): "Крещенский сочельник",
        (9, 11): "Усекновение главы Иоанна Предтечи",
        (9, 27): "Воздвижение Креста Господня",
    }
    return fixed.get((d.month, d.day))


def describe(d: date) -> dict:
    """Что за день: постный ли, почему, праздник ли."""
    info = {
        "date": d.isoformat(),
        "weekday": RU_WEEKDAYS[d.weekday()],
        "fast": False,
        "fast_reason": None,
        "feast": feast(d),
        "note": None,
        "pascha_this_year": pascha(d.year).isoformat(),
    }
    strict = strict_one_day_fast(d)
    multi = multi_day_fast(d)
    free = fast_free_week(d)

    if strict:
        info["fast"], info["fast_reason"] = True, f"строгий однодневный пост: {strict}"
    elif multi:
        info["fast"], info["fast_reason"] = True, multi
    elif free:
        info["note"] = free
    elif d.weekday() in (2, 4):
        info["fast"], info["fast_reason"] = True, f"{RU_WEEKDAYS[d.weekday()]} — постный день"

    if info["feast"] and info["fast"]:
        info["note"] = "праздник в постный день — мера поста обычно смягчается, уточнить у духовника"
    return info


def human(d: date) -> str:
    """Короткая строка для напоминания."""
    i = describe(d)
    parts = []
    if i["feast"]:
        parts.append(f"праздник: {i['feast']}")
    if i["fast"]:
        parts.append(f"постный день ({i['fast_reason']})")
    elif i["note"]:
        parts.append(i["note"])
    else:
        parts.append("поста нет")
    return f"{d.strftime('%d.%m')}, {i['weekday']}: " + "; ".join(parts)
