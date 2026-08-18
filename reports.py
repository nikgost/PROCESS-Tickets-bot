"""Сборка текста отчётов «На сегодня куплено билетов…» и формат расписаний."""

from __future__ import annotations  # чтобы код работал и на Python 3.9

import html
import logging
import re
from datetime import datetime
from zoneinfo import ZoneInfo

import config
import db
from qtickets import QTicketsClient, QTicketsError

log = logging.getLogger(__name__)

DAY_NAMES = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]

# --- Разбор дней недели и времени, введённых текстом ---

# Одно слово — один день. Учтены сокращения, полные названия и падежи.
_DAY_WORDS = {
    "пн": 0, "по": 0, "понедельник": 0, "понедельники": 0, "mon": 0, "monday": 0,
    "вт": 1, "вторник": 1, "вторники": 1, "tue": 1, "tuesday": 1,
    "ср": 2, "среда": 2, "среду": 2, "среды": 2, "wed": 2, "wednesday": 2,
    "чт": 3, "четверг": 3, "четверги": 3, "thu": 3, "thursday": 3,
    "пт": 4, "пятница": 4, "пятницу": 4, "пятницы": 4, "fri": 4, "friday": 4,
    "сб": 5, "суббота": 5, "субботу": 5, "субботы": 5, "sat": 5, "saturday": 5,
    "вс": 6, "вск": 6, "воскресенье": 6, "воскресенья": 6, "sun": 6, "sunday": 6,
}

# Слово — сразу набор дней.
_DAY_GROUPS = {
    "каждыйдень": 0b1111111, "каждый": 0b1111111, "ежедневно": 0b1111111,
    "всегда": 0b1111111, "все": 0b1111111, "вседни": 0b1111111, "всюнеделю": 0b1111111,
    "будни": 0b0011111, "рабочие": 0b0011111, "рабочиедни": 0b0011111,
    "выходные": 0b1100000, "выхи": 0b1100000,
}


# Слова, которые в ответе можно просто пропустить.
_FILLER_WORDS = {"день", "дни", "дня", "недели", "неделю", "по", "в", "во", "и", "а", "также"}


def parse_days(text: str) -> tuple[int | None, str | None]:
    """Разобрать дни недели из текста вроде «вт, ср, чт», «пн-пт», «будни».

    Возвращает (набор дней, непонятое слово). Если всё разобрано — второе None.
    """
    if not text:
        return None, None
    cleaned = text.lower().replace("ё", "е").strip()
    for sep in (",", ";", "/", "\\", "|", "+", " и ", ".", "\n"):
        cleaned = cleaned.replace(sep, " ")
    for dash in ("—", "–", "‒", "−"):
        cleaned = cleaned.replace(dash, "-")

    mask = 0
    # Сначала пробуем всю строку целиком — так ловятся фразы из двух слов
    # («каждый день», «все дни», «рабочие дни»).
    whole = cleaned.replace(" ", "").replace("-", "")
    if whole in _DAY_GROUPS:
        return _DAY_GROUPS[whole], None

    for token in cleaned.split():
        token = token.strip("-").strip()
        if not token or token in _FILLER_WORDS:
            continue
        if token in _DAY_GROUPS:
            mask |= _DAY_GROUPS[token]
            continue
        if "-" in token:  # диапазон, например «пн-чт» или «пт-вс»
            left, _, right = token.partition("-")
            if left in _DAY_WORDS and right in _DAY_WORDS:
                start, end = _DAY_WORDS[left], _DAY_WORDS[right]
                day = start
                while True:
                    mask |= 1 << day
                    if day == end:
                        break
                    day = (day + 1) % 7
                continue
            return None, token
        if token in _DAY_WORDS:
            mask |= 1 << _DAY_WORDS[token]
            continue
        return None, token

    return (mask or None), None


def rotate_days(mask: int, shift: int) -> int:
    """Сдвинуть отмеченные дни недели на shift дней (при переходе через полночь)."""
    if not mask or shift % 7 == 0:
        return mask
    result = 0
    for day in range(7):
        if (mask >> day) & 1:
            result |= 1 << ((day + shift) % 7)
    return result


def shift_schedule(
    mask: int | None,
    send_time: str | None,
    old_tz: str,
    new_tz: str,
) -> tuple[int | None, str | None]:
    """Пересчитать расписание при смене часового пояса чата.

    Момент отправки остаётся тем же самым: 13:05 по Москве — это 15:05 по
    Екатеринбургу. Если при пересчёте время перешло через полночь, дни недели
    сдвигаются вместе с ним.
    """
    if not mask or not send_time or old_tz == new_tz:
        return mask, send_time
    try:
        old_zone, new_zone = ZoneInfo(old_tz), ZoneInfo(new_tz)
    except Exception:
        return mask, send_time
    try:
        hour, minute = (int(part) for part in send_time.split(":"))
    except ValueError:
        return mask, send_time

    today = datetime.now(old_zone).date()
    was = datetime(today.year, today.month, today.day, hour, minute, tzinfo=old_zone)
    now_local = was.astimezone(new_zone)
    day_shift = (now_local.date() - was.date()).days
    return rotate_days(mask, day_shift), now_local.strftime("%H:%M")


def parse_time(text: str) -> str | None:
    """Разобрать время из «18:32», «18.32», «1832», «18 32», «9» и т. п."""
    if not text:
        return None
    cleaned = text.lower().strip()
    for word in ("в", "часов", "час", "ч", "мин", "минут"):
        cleaned = cleaned.replace(word, " ")
    digits_only = "".join(ch for ch in cleaned if ch.isdigit() or ch in " :.-,")
    for sep in (".", "-", ",", " "):
        digits_only = digits_only.replace(sep, ":")
    parts = [p for p in digits_only.split(":") if p != ""]

    if len(parts) == 1:
        chunk = parts[0]
        if len(chunk) in (3, 4) and chunk.isdigit():  # «1832» или «932»
            parts = [chunk[:-2], chunk[-2:]]
        elif chunk.isdigit() and len(chunk) <= 2:  # «18» — значит ровно 18:00
            parts = [chunk, "0"]
        else:
            return None
    if len(parts) != 2 or not all(p.isdigit() for p in parts):
        return None

    hour, minute = int(parts[0]), int(parts[1])
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return f"{hour:02d}:{minute:02d}"


# Городская пометка в конце названия: «(Екб)», «[Пермь]» и тому подобное.
# Длину содержимого ограничиваем, чтобы не срезать осмысленный подзаголовок
# вроде «(концерт в двух отделениях)» — в любое название города это влезает
# с запасом («Нижний Новгород» — 15 знаков).
_CITY_TAG = re.compile(r"\s*[\(\[]([^()\[\]]{1,24})[\)\]]\s*$")


def display_name(name: str) -> str:
    """Название спектакля для сообщения — без городской пометки в конце.

    «Леопарды Килиманджаро (Екб)» → «Леопарды Килиманджаро».
    Работает для любого города: убирается сама скобка в конце, а не список
    заранее известных городов. Если после этого от названия ничего не
    останется, возвращаем его как было — лучше с пометкой, чем пустым.

    Полное название при этом никуда не девается: в меню, в списке добавления
    и в карточке мероприятия оно показывается целиком, иначе одноимённые
    спектакли разных городов стало бы не различить.
    """
    cleaned = (name or "").strip()
    while True:
        match = _CITY_TAG.search(cleaned)
        if match is None:
            break
        candidate = cleaned[: match.start()].strip()
        if not candidate:
            break
        cleaned = candidate
    return cleaned or (name or "").strip()


def format_days(mask: int | None) -> str:
    if not mask:
        return "—"
    if mask >= 127:
        return "каждый день"
    return ", ".join(name for i, name in enumerate(DAY_NAMES) if (mask >> i) & 1) or "—"


def format_schedule(mask: int | None, send_time: str | None) -> str | None:
    """Например: «Вт, Ср, Чт в 14:00». Если расписание не задано — None."""
    if not mask or not send_time:
        return None
    return f"{format_days(mask)} в {send_time}"


def effective_schedule(event_row, chat_row) -> tuple[int | None, str | None, bool]:
    """Действующее расписание мероприятия: своё, если задано, иначе общее для чата.

    Возвращает (дни, время, своё_ли_расписание).
    """
    if event_row["send_time"] and event_row["days_mask"]:
        return event_row["days_mask"], event_row["send_time"], True
    return chat_row["days_mask"], chat_row["send_time"], False


def _tzinfo(tz_name: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(tz_name or config.DEFAULT_TZ)
    except Exception:
        return ZoneInfo(config.DEFAULT_TZ)


async def event_report_block(
    qt: QTicketsClient, event_id: int, tz_name: str | None
) -> tuple[str, str]:
    """Отчёт по одному мероприятию за сегодня.

    Возвращает (свежее название, текст блока). Может выбросить QTicketsError.
    """
    info = await qt.get_event(event_id)
    tz = _tzinfo(tz_name)
    today = datetime.now(tz).date()

    todays = []
    for s in info["shows"]:
        start = s["start"]
        if start.tzinfo is None:
            # На случай, если дата придёт без часового пояса — считаем её местной
            start = start.replace(tzinfo=tz)
        local = start.astimezone(tz)
        if local.date() == today:
            todays.append({"id": s["id"], "local": local})

    if not todays:
        return info["name"], "Сегодня сеансов нет."

    counts = await qt.count_paid_tickets(event_id, {t["id"] for t in todays})
    total = sum(counts.values())
    if total == 0:
        return info["name"], "На сегодня куплено билетов: 0"

    # В отчёт попадают только сеансы, на которые уже есть зрители;
    # сеансы с нулём не показываем.
    todays.sort(key=lambda t: t["local"])
    lines = ["На сегодня куплено билетов:"]
    for t in todays:
        n = counts.get(t["id"], 0)
        if n > 0:
            lines.append(f"{t['local'].strftime('%H:%M')} — {n}")
    return info["name"], "\n".join(lines)


async def build_report(
    qt: QTicketsClient,
    chat_row,
    events: list,
    only_event_ids: set[int] | None = None,
) -> str:
    """Готовый текст отчёта по чату (все мероприятия или только указанные).

    Перед строками с билетами всегда ставится название мероприятия
    (жирным). Ошибка по одному мероприятию не ломает отчёт по остальным.
    """
    if not events:
        return (
            "В этом чате пока нет мероприятий.\n"
            "Откройте /menu и нажмите «Добавить мероприятие»."
        )

    chosen = [
        ev
        for ev in events
        if only_event_ids is None or ev["event_id"] in only_event_ids
    ]
    if not chosen:
        return "Нет мероприятий для отчёта."

    # Название мероприятия жирным ставим всегда — и когда оно одно, и когда
    # в одном сообщении собрано несколько.
    parts = []
    for ev in chosen:
        try:
            fresh_name, block = await event_report_block(
                qt, ev["event_id"], chat_row["tz"]
            )
            name = fresh_name
            db.update_event_name(chat_row["chat_id"], ev["event_id"], fresh_name)
        except QTicketsError as e:
            name = ev["name"] or f"Мероприятие {ev['event_id']}"
            block = f"⚠️ {e}"
        except Exception:
            log.exception("Сбой отчёта по мероприятию %s", ev["event_id"])
            name = ev["name"] or f"Мероприятие {ev['event_id']}"
            block = "⚠️ Внутренняя ошибка при сборке отчёта. Подробности в журнале бота."

        title = display_name(str(name))
        parts.append(f"<b>{html.escape(title)}</b>\n{html.escape(block)}")

    result = "\n\n".join(parts)
    if len(result) > 4000:  # предел Телеграма на одно сообщение — 4096 знаков
        result = result[:3990] + "\n…"
    return result
