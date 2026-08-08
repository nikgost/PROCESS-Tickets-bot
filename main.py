"""Запуск бота: python main.py

Бот работает по принципу «длинного опроса»: сам регулярно спрашивает
Телеграм о новых сообщениях. Поэтому серверу не нужны ни свой домен,
ни сертификаты — достаточно доступа в интернет.
"""

from __future__ import annotations  # чтобы код работал и на Python 3.9

import asyncio
import logging
import sys

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand

import config
import db
import handlers
import scheduler

log = logging.getLogger(__name__)


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    errors = config.validate()
    if errors:
        print("Бот не запущен — исправьте настройки в файле .env:\n", file=sys.stderr)
        for e in errors:
            print(f"  • {e}", file=sys.stderr)
        raise SystemExit(1)

    db.init()

    bot = Bot(
        token=config.BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(handlers.router)

    await bot.set_my_commands(
        [
            BotCommand(command="menu", description="Главное меню"),
            BotCommand(command="report", description="Отчёт за сегодня"),
            BotCommand(command="help", description="Справка"),
        ]
    )

    me = await bot.get_me()
    log.info("Запущен бот @%s (id %s)", me.username, me.id)

    # Ссылку на задачу держим в переменной: иначе сборщик мусора Python
    # вправе убрать её вместе с планировщиком уведомлений.
    scheduler_task = asyncio.create_task(scheduler.run(bot))
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        scheduler_task.cancel()


if __name__ == "__main__":
    if sys.version_info < (3, 9):
        print(
            "Нужен Python 3.9 или новее. Сейчас: "
            f"{sys.version_info.major}.{sys.version_info.minor}.\n"
            "На Ubuntu 20.04 поставить: apt install -y python3.9 python3.9-venv",
            file=sys.stderr,
        )
        raise SystemExit(1)
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
