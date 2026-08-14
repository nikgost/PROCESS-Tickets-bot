"""Отправка уведомлений по расписанию.

Устройство простое и устойчивое к перезапускам: раз в ~20 секунд бот
проверяет по базе, у каких чатов и мероприятий наступила минута отправки
(по часовому поясу каждого чата). Чтобы одно и то же уведомление не ушло
дважды, каждая отправка записывается в журнал sent_log.
"""

from __future__ import annotations  # чтобы код работал и на Python 3.9

import asyncio
import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError

import cleanup
import config
import db
import qtickets
import reports

log = logging.getLogger(__name__)

TICK_SECONDS = 20


async def run(bot: Bot) -> None:
    await asyncio.sleep(5)  # даём боту спокойно запуститься
    last_cleanup_day = None
    while True:
        try:
            await _tick(bot)
        except Exception:
            log.exception("Сбой в планировщике")
        try:
            # Уборка служебных сообщений: очередь лежит в базе, поэтому
            # переживает перезапуск бота.
            await cleanup.run_due(bot)
        except Exception:
            log.exception("Сбой уборки служебных сообщений")
        try:
            today = datetime.now(timezone.utc).date()
            if last_cleanup_day != today:
                db.sent_cleanup(days=40)
                last_cleanup_day = today
        except Exception:
            log.exception("Сбой очистки журнала отправок")
        await asyncio.sleep(TICK_SECONDS)


def _tz(name: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(name or config.DEFAULT_TZ)
    except Exception:
        return ZoneInfo(config.DEFAULT_TZ)


async def _tick(bot: Bot) -> None:
    for chat in db.enabled_chats():
        chat_id = chat["chat_id"]
        now = datetime.now(_tz(chat["tz"]))
        hhmm = now.strftime("%H:%M")
        weekday_bit = 1 << now.weekday()  # 0 = понедельник
        local_date = now.date().isoformat()

        events = db.get_events(chat_id)
        due = []
        for ev in events:
            mask, send_time, _own = reports.effective_schedule(ev, chat)
            if not mask or not send_time:
                continue
            if send_time != hhmm or not (mask & weekday_bit):
                continue
            if db.sent_exists(chat_id, ev["event_id"], local_date, hhmm):
                continue
            due.append(ev)

        if not due:
            continue

        # Каждому спектаклю — отдельное сообщение. Так в уведомлении на
        # заблокированном экране видно название именно этого спектакля:
        # в push попадает начало текста, а начинается он с названия.
        for ev in due:
            try:
                text = await reports.build_report(
                    qtickets.get_client(),
                    chat,
                    events,
                    only_event_ids={ev["event_id"]},
                )
            except Exception:
                log.exception("Сбой сборки отчёта для чата %s", chat_id)
                text = (
                    "⚠️ Не получилось собрать отчёт о билетах. "
                    "Подробности в журнале бота."
                )

            try:
                await bot.send_message(chat_id, text)
            except TelegramForbiddenError:
                log.warning(
                    "Чат %s недоступен (бота убрали?) — выключаю уведомления", chat_id
                )
                db.set_enabled(chat_id, 0)
                break
            except TelegramBadRequest as e:
                log.warning("Не удалось отправить сообщение в чат %s: %s", chat_id, e)
                continue

            db.sent_add(chat_id, ev["event_id"], local_date, hhmm)
            log.info(
                "Отправлен отчёт в чат %s по мероприятию %s", chat_id, ev["event_id"]
            )
