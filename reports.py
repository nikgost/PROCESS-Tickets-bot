"""Сборка текста отчётов «На сегодня куплено билетов…» и формат расписаний."""

import html
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

import config
import db
from qtickets import QTicketsClient, QTicketsError

log = logging.getLogger(__name__)

DAY_NAMES = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]


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

        parts.append(f"<b>{html.escape(str(name))}</b>\n{html.escape(block)}")

    result = "\n\n".join(parts)
    if len(result) > 4000:  # предел Телеграма на одно сообщение — 4096 знаков
        result = result[:3990] + "\n…"
    return result
