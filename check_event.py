"""Проверка: что QTickets на самом деле отдаёт по мероприятию.

Ничего не меняет и никуда не пишет — только показывает данные, чтобы понять,
почему бот считает какой-то сеанс сегодняшним.

Запуск на сервере:

    cd /opt/PROCESS-Tickets-bot
    venv/bin/python check_event.py 186377

Часовой пояс берётся из настроек того чата, куда мероприятие добавлено в боте.
Если мероприятие не добавлено ни в один чат — московский. Другой пояс можно
указать вторым словом:

    venv/bin/python check_event.py 186377 Europe/Moscow
"""

from __future__ import annotations  # чтобы код работал и на Python 3.9

import asyncio
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import config
import qtickets

DEFAULT_TZ = "Europe/Moscow"


def _fmt(value) -> str:
    return "—" if value is None else str(value)


def _chat_tz(event_id: int) -> str:
    """Пояс чата, куда мероприятие добавлено в боте; иначе московский."""
    try:
        import sqlite3
        con = sqlite3.connect(config.DB_PATH)
        row = con.execute(
            "SELECT c.tz FROM chat_events e JOIN chats c ON c.chat_id = e.chat_id "
            "WHERE e.event_id = ? AND c.tz IS NOT NULL LIMIT 1",
            (int(event_id),),
        ).fetchone()
        con.close()
        if row and row[0]:
            return row[0]
    except Exception:
        pass
    return DEFAULT_TZ


def _skip_reason(show: dict) -> str:
    """Почему бот пропускает сеанс (пусто — не пропускает).

    Выключенный сеанс бот НЕ пропускает: билеты на него считаются. Он лишь
    не упоминается в чате, если билетов на него нет.
    """
    if show.get("deleted_at"):
        return "удалён (deleted_at)"
    return ""


async def main() -> None:
    if len(sys.argv) < 2 or not sys.argv[1].isdigit():
        print("Укажите ID мероприятия, например:")
        print("    venv/bin/python check_event.py 186377")
        raise SystemExit(1)

    event_id = int(sys.argv[1])
    tz_name = sys.argv[2] if len(sys.argv) > 2 else _chat_tz(event_id)
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

    # Сегодняшние сеансы собираем по ВСЕМУ списку, а печатаем только
    # окрестность сегодняшнего дня: за 3 дня до и 3 дня после.
    counted = [
        int(s["id"]) for local, s in rows
        if local is not None and local.date() == today and not _skip_reason(s)
    ]
    near = [
        (local, s) for local, s in rows
        if local is not None and abs((local.date() - today).days) <= 3
    ]
    print(f"Сеансы с {today - timedelta(days=3)} по {today + timedelta(days=3)}:")
    if not near:
        print("  в эти дни сеансов нет")
    for local, s in near:
        skip = _skip_reason(s)
        is_today = local.date() == today and not skip

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
        if s.get("is_active") in (0, False, "0") and not skip:
            line += "  [выключен: билеты считаются, без билетов в чате не упоминается]"
        if is_today:
            line += "   <<< СЧИТАЕТСЯ СЕГОДНЯШНИМ"
        print(line)

    print()
    if not counted:
        print("Сегодняшних сеансов нет — бот должен написать «Сегодня сеансов нет».")
        return
    print(f"Бот считает сегодняшними сеансы: {counted}")
    await compare_counts(event_id, counted, rows)


async def compare_counts(event_id: int, show_ids: list, rows: list) -> None:
    """Сравнить два способа подсчёта и объяснить каждый расхождённый билет."""
    client = qtickets.get_client()
    times = {int(s["id"]): local for local, s in rows if local is not None}

    # Способ бота сейчас: действующие билеты сеанса (как в кабинете QTickets)
    by_show = {}
    for sid in show_ids:
        resp = await client._request("GET", f"shows/{sid}/barcodes")
        items = resp.get("data") if isinstance(resp, dict) else resp
        by_show[sid] = [it for it in (items or []) if isinstance(it, dict)]

    # Прежний способ: оплаченные заказы. Заодно запоминаем, в каком заказе какой билет.
    basket_order = {}
    counted_old = set()
    page, capped = 1, False
    while True:
        if page > 30:
            capped = True
            break
        resp = await client._request("GET", "orders", {
            "where": [{"column": "event_id", "value": int(event_id)}],
            "orderBy": {"id": "desc"},
            "page": page,
        })
        items, paging = client._data_list(resp)
        if not items:
            break
        for order in items:
            if not isinstance(order, dict):
                continue
            for b in order.get("baskets") or []:
                if not isinstance(b, dict) or b.get("id") is None:
                    continue
                basket_order[int(b["id"])] = (order, b)
                ok = (
                    order.get("payed") and not order.get("deleted_at")
                    and not b.get("deleted_at") and not b.get("refunded_at")
                )
                if ok:
                    counted_old.add(int(b["id"]))
        per_page = int(paging.get("perPage") or 100) if paging else 100
        total = int(paging.get("total") or 0) if paging else 0
        if (total and page * per_page >= total) or len(items) < per_page:
            break
        page += 1

    print()
    print("Сравнение по сегодняшним сеансам:")
    print("  время   | в кабинете (так считает бот теперь) | по оплаченным заказам (как было)")
    for sid in show_ids:
        ids = {int(it["id"]) for it in by_show[sid] if it.get("id") is not None}
        old_n = len(ids & counted_old)
        t = times.get(sid)
        print(f"  {t:%H:%M}   | {len(ids):^35} | {old_n:^10}" if t else f"  {sid} | {len(ids)} | {old_n}")

    print()
    lost = []
    for sid in show_ids:
        for it in by_show[sid]:
            bid = it.get("id")
            if bid is not None and int(bid) not in counted_old:
                lost.append((sid, it))
    if not lost:
        print("Расхождений нет: оба способа видят одни и те же билеты.")
        return

    print(f"Билеты, которые прежний способ НЕ видел ({len(lost)} шт.), и почему:")
    for sid, it in lost:
        t = times.get(sid)
        head = f"  {t:%H:%M}" if t else f"  сеанс {sid}"
        pair = basket_order.get(int(it["id"]))
        if pair is None:
            print(f"{head}  билет {it['id']}: его заказа нет в списке заказов мероприятия"
                  " — вероятно, продан через партнёра или оформлен в обход заказов")
            continue
        order, b = pair
        reasons = []
        if not order.get("payed"):
            reasons.append("заказ не отмечен как оплаченный")
        if order.get("promo_code_id"):
            reasons.append(f"промокод/сертификат (promo_code_id={order.get('promo_code_id')})")
        if order.get("discount_id"):
            reasons.append(f"скидка (discount_id={order.get('discount_id')})")
        if order.get("backend_user_id"):
            reasons.append("оформлен из личного кабинета (приглашение/ручная выдача)")
        if order.get("reserved"):
            reasons.append("бессрочная бронь")
        print(f"{head}  билет {it['id']}, заказ {order.get('id')}, цена {order.get('price')}: "
              + ("; ".join(reasons) if reasons else "причина не видна по полям заказа"))
    if capped:
        print("  (просмотрены только последние заказы — для сегодняшних сеансов этого достаточно)")


if __name__ == "__main__":
    asyncio.run(main())
