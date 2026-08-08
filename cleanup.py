"""Уборка за собой: служебные сообщения бота исчезают из чата сами.

Что считается служебным: меню, экраны настройки, приветствие при добавлении
в чат, приглашение ввести ID и короткие пояснения. Отчёты о билетах —
и присланные по расписанию, и запрошенные кнопкой — не удаляются никогда.

Права администратора для этого не нужны: свои собственные сообщения бот
может удалять в любом чате. Чужие сообщения бот не трогает вообще.

Ограничение Телеграма: удалить можно только то, что отправлено менее
48 часов назад. Нам хватает с запасом — всё убирается за минуты.
"""

from __future__ import annotations  # чтобы код работал и на Python 3.9

import asyncio
import logging

log = logging.getLogger(__name__)

# Сколько живёт экран меню без нажатий (отсчёт начинается заново при каждом
# нажатии на кнопку, так что настроить расписание никто не помешает).
MENU_LIFETIME = 300
# Короткие пояснения и приветствие.
NOTICE_LIFETIME = 60

# Отложенные удаления: (чат, сообщение) -> задача
_tasks: dict[tuple[int, int], asyncio.Task] = {}
# Живое меню чата: чат -> сообщение (в чате всегда не больше одного меню)
_live_menu: dict[int, int] = {}


async def _delete_later(bot, chat_id: int, message_id: int, delay: int) -> None:
    try:
        await asyncio.sleep(delay)
        await bot.delete_message(chat_id, message_id)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        # Сообщение уже удалено вручную, слишком старое или удалять нечего —
        # это не ошибка работы бота, просто пропускаем.
        log.debug("Не удалось удалить сообщение %s в чате %s: %s", message_id, chat_id, e)
    finally:
        _tasks.pop((chat_id, message_id), None)


def schedule(bot, chat_id: int, message_id: int, delay: int = MENU_LIFETIME) -> None:
    """Убрать сообщение через delay секунд. Повторный вызов сдвигает срок."""
    cancel(chat_id, message_id)
    _tasks[(chat_id, message_id)] = asyncio.create_task(
        _delete_later(bot, chat_id, message_id, delay)
    )


def cancel(chat_id: int, message_id: int) -> None:
    """Отменить запланированное удаление (например, сообщение стало отчётом)."""
    task = _tasks.pop((chat_id, message_id), None)
    if task is not None:
        task.cancel()


async def delete_now(bot, chat_id: int, message_id: int) -> None:
    cancel(chat_id, message_id)
    try:
        await bot.delete_message(chat_id, message_id)
    except Exception as e:
        log.debug("Не удалось удалить сообщение %s в чате %s: %s", message_id, chat_id, e)


async def register_menu(bot, chat_id: int, message_id: int) -> None:
    """Запомнить новое меню чата и убрать предыдущее, чтобы они не копились."""
    previous = _live_menu.get(chat_id)
    if previous is not None and previous != message_id:
        await delete_now(bot, chat_id, previous)
    _live_menu[chat_id] = message_id
    schedule(bot, chat_id, message_id, MENU_LIFETIME)


def forget_menu(chat_id: int, message_id: int) -> None:
    if _live_menu.get(chat_id) == message_id:
        _live_menu.pop(chat_id, None)
    cancel(chat_id, message_id)
