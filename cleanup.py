"""Уборка за собой: служебные сообщения бота исчезают из чата сами.

Что считается служебным: меню, экраны настройки, приветствие при добавлении
в чат, вопросы бота и короткие пояснения. Отчёты о билетах — и присланные
по расписанию, и запрошенные кнопкой — не удаляются никогда.

Как это устроено. Очередь на удаление хранится в базе, а не в памяти:
раз в 20 секунд планировщик заглядывает в неё и убирает то, чему подошёл
срок. Поэтому перезапуск бота (при обновлении или перезагрузке сервера)
больше не оставляет в чате брошенные меню — очередь на месте, и уборка
продолжится сама.

Свои собственные сообщения бот вправе удалять в любом чате без всяких прав.
Чужие (ваши команды, ваши ответы на вопросы бота) Телеграм разрешает
удалять только администратору с правом «Удаление сообщений» — если права
нет, сообщение просто останется, и это не считается ошибкой.

Ещё ограничение Телеграма: удалить можно только то, что отправлено менее
48 часов назад. Записи старше этого срока из очереди отбрасываются.
"""

from __future__ import annotations  # чтобы код работал и на Python 3.9

import logging
import time

import db

log = logging.getLogger(__name__)

# Сколько живёт экран меню без нажатий (отсчёт начинается заново при каждом
# нажатии на кнопку, так что настроить расписание никто не помешает).
MENU_LIFETIME = 300
# Короткие пояснения и приветствие.
NOTICE_LIFETIME = 60

# Телеграм не даёт удалять сообщения старше 48 часов — берём с запасом.
MAX_AGE = 47 * 3600

# Чаты, где бот не смог убрать сообщение собеседника (не выдано право
# администратора). Нужно только для подсказки в меню; после перезапуска
# определяется заново при первой же команде.
no_delete_rights = set()


def schedule(chat_id: int, message_id: int, delay: int = MENU_LIFETIME) -> None:
    """Убрать сообщение через delay секунд. Повторный вызов сдвигает срок."""
    db.delete_plan(chat_id, message_id, int(time.time()) + int(delay))


def cancel(chat_id: int, message_id: int) -> None:
    """Отменить запланированное удаление — сообщение останется в чате."""
    db.delete_forget(chat_id, message_id)


async def delete_now(bot, chat_id: int, message_id: int) -> bool:
    """Удалить сразу. Возвращает True, если Телеграм это позволил."""
    cancel(chat_id, message_id)
    try:
        await bot.delete_message(chat_id, message_id)
        return True
    except Exception as e:
        # Сообщение уже удалено вручную, слишком старое или у бота нет прав —
        # это не ошибка работы бота, просто идём дальше.
        log.debug("Не удалось удалить сообщение %s в чате %s: %s", message_id, chat_id, e)
        return False


async def register_menu(bot, chat_id: int, message_id: int) -> None:
    """Запомнить новое меню чата и убрать предыдущее, чтобы они не копились."""
    previous = db.menu_get(chat_id)
    if previous is not None and previous != message_id:
        await delete_now(bot, chat_id, previous)
    db.menu_set(chat_id, message_id)
    schedule(chat_id, message_id, MENU_LIFETIME)


def forget_menu(chat_id: int, message_id: int) -> None:
    """Сообщение больше не меню и не удаляется (так помечены отчёты)."""
    db.menu_clear(chat_id, message_id)
    cancel(chat_id, message_id)


async def run_due(bot) -> None:
    """Убрать всё, чему подошёл срок. Вызывается планировщиком раз в 20 секунд."""
    now = int(time.time())
    try:
        db.delete_purge_old(now - MAX_AGE)
        due = db.delete_due(now)
    except Exception:
        log.exception("Не удалось прочитать очередь удаления")
        return

    for row in due:
        chat_id, message_id = row["chat_id"], row["message_id"]
        # Запись убираем в любом случае: если удалить не вышло, повторные
        # попытки ничего не изменят и только копили бы очередь.
        db.delete_forget(chat_id, message_id)
        db.menu_clear(chat_id, message_id)
        try:
            await bot.delete_message(chat_id, message_id)
        except Exception as e:
            log.debug(
                "Не удалось удалить сообщение %s в чате %s: %s", message_id, chat_id, e
            )
