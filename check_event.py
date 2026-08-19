"""Проверка: что QTickets на самом деле отдаёт по мероприятию.

Ничего не меняет и никуда не пишет — только показывает данные, чтобы понять,
почему бот считает какой-то сеанс сегодняшним.

Запуск на сервере:

    cd /opt/PROCESS-Tickets-bot
    venv/bin/python check_event.py 186377

Часовой пояс по умолчанию — екатеринбургский (он же пермский). Другой можно
указать вторым словом:

    venv/bin/python check_event.py 186377 Europe/Moscow
"""

from __future__ import annotations  # чтобы код работал и на Python 3.9

import asyncio
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

import config
import qtickets

DEFAULT_TZ = "Asia/Yekaterinburg"


def _fmt(value) -> str:
    return "—" if value is None else str(value)


def _skip_reason(show: dict) -> str:
    """Почему бот пропускает сеанс (пусто — не пропускает)."""
    if show.get("is_active") in (0, False, "0"):
        return "выключен (is_active)"
    if show.get("deleted_at"):
        return "удалён (deleted_at)"
    return ""


async def main() -> None:
    if len(sys.argv) < 2 or not sys.argv[1].isdigit():
        print("Укажите ID мероприятия, например:")
        print("    venv/bin/python check_event.py 186377")
        raise SystemExit(1)

    event_id = int(sys.argv[1])
    tz_name = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_TZ
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        print(f"Не знаю такого часового пояса: {tz_name}")
        raise SystemExit(1)

    errors = config.validate()
    if errors:
        print("Сначала исправьте настройки в .env:")
        for e in errors:
            print(f"  • {e}")
        raise SystemExit(1)

    now = datetime.now(tz)
    print(f"Сейчас по поясу {tz_name}: {now:%Y-%m-%d %H:%M}")
    print(f"«Сегодня» для бота: {now.date()}")
    print()

    resp = await qtickets.get_client()._request("GET", f"events/{event_id}")
    data = resp.get("data") if isinstance(resp, dict) else None
    if not isinstance(data, dict):
        print("QTickets не вернул данные мероприятия. Ответ целиком:")
        print(resp)
        raise SystemExit(1)

    print(f"Мероприятие: {data.get('name')!r}  (id {data.get('id')})")
    shows = [s for s in (data.get("shows") or []) if isinstance(s, dict)]
    print(f"Всего сеансов в карточке: {len(shows)}")
    if shows:
        print("Поля сеанса: " + ", ".join(sorted(shows[0].keys())))
    print()

    today = now.date()
    rows = []
    for s in shows:
        start = qtickets._parse_dt(s.get("start_date"))
        if start is None:
            local = None
        elif start.tzinfo is None:
            local = start.replace(tzinfo=tz)
        else:
            local = start.astimezone(tz)
        rows.append((local, s))
    rows.sort(key=lambda r: (r[0] is None, r[0]))

    counted = []
    print("Сеансы (последние 60 по дате):")
    for local, s in rows[-60:]:
        skip = _skip_reason(s)
        is_today = local is not None and local.date() == today and not skip
        if is_today:
            counted.append(int(s["id"]))

        local_text = f"{local:%Y-%m-%d %H:%M}" if local else "дата не разобрана"
        line = (
            f"  id={_fmt(s.get('id')):>8}"
            f"  из QTickets: {_fmt(s.get('start_date')):<28}"
            f"  по поясу чата: {local_text}"
        )
        if s.get("is_active") is not None:
            line += f"  is_active={s.get('is_active')}"
        if s.get("deleted_at"):
            line += f"  deleted_at={s.get('deleted_at')}"
        if skip:
            line += f"  [бот пропускает: {skip}]"
        if is_today:
            line += "   <<< СЧИТАЕТСЯ СЕГОДНЯШНИМ"
        print(line)

    print()
    if counted:
        print(f"Бот считает сегодняшними сеансы: {counted}")
        print("Поэтому в отчёте не «Сегодня сеансов нет», а число билетов.")
    else:
        print("Сегодняшних сеансов нет — бот должен написать «Сегодня сеансов нет».")


if __name__ == "__main__":
    asyncio.run(main())
