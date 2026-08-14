"""Обработка команд и нажатий на кнопки."""

from __future__ import annotations  # чтобы код работал и на Python 3.9

import html
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from aiogram import BaseMiddleware, Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, ChatMemberUpdated, ForceReply, Message

import cleanup
import config
import db
import keyboards
import qtickets
import reports
from qtickets import QTicketsError

log = logging.getLogger(__name__)
router = Router()

CONFIRM_FLOW_BASE = (
    "Вы уверены, что хотите отменить текущую активность и перейти в главное меню?\n\n"
    "Сейчас ожидается: {what}"
)

_WAITING_HINTS = {
    "AddEventState:waiting_id": "отправьте ID мероприятия.",
    "ScheduleInput:waiting_time": "напишите время отправки.",
}


def confirm_flow_text(state_name: str | None) -> str:
    what = _WAITING_HINTS.get(state_name or "", "завершите текущее действие.")
    return CONFIRM_FLOW_BASE.format(what=what)

HELP_TEXT = (
    "<b>Что я умею</b>\n\n"
    "Я присылаю в чат отчёты о купленных билетах из QTickets — по расписанию "
    "и по запросу.\n\n"
    "Команды:\n"
    "/menu — главное меню (все настройки — кнопками)\n"
    "/report — отчёт за сегодня прямо сейчас\n"
    "/help — эта справка\n\n"
    "Как это устроено:\n"
    "• В каждый чат можно добавить несколько мероприятий (по их ID из QTickets).\n"
    "• Расписание уведомлений бывает общим для чата и своим у каждого "
    "мероприятия. Своё расписание важнее общего.\n"
    "• «Сегодня» и время отправки бот считает по часовому поясу чата "
    "(меняется в меню)."
)


class AddEventState(StatesGroup):
    waiting_id = State()


class ScheduleInput(StatesGroup):
    """Дни выбираются кнопками, время вводится текстом."""

    waiting_time = State()


TIME_PROMPT = (
    "⏰ <b>Во сколько присылать отчёт?</b>\n"
    "Напишите время ответом на это сообщение — например <code>18:32</code>.\n\n"
    "Дни: {days}\n"
    "Пояс чата: {tz}"
)


# ---------- Вспомогательные ----------

async def safe_edit(msg: Message, text: str, kb=None, ephemeral: bool = True) -> None:
    """Изменить сообщение.

    ephemeral=True (по умолчанию) — это служебный экран: после правки заново
    заводим отсчёт до его уборки. ephemeral=False — сообщение остаётся в чате
    навсегда (так помечены отчёты о билетах).

    Ошибки «текст не изменился» и «сообщение не найдено» пропускаем молча:
    экран мог быть уже убран или удалён вручную.
    """
    try:
        await msg.edit_text(text, reply_markup=kb)
    except TelegramBadRequest as e:
        text_err = str(e).lower()
        if "message is not modified" in text_err:
            pass
        elif "message to edit not found" in text_err or "message can't be edited" in text_err:
            cleanup.forget_menu(msg.chat.id, msg.message_id)
            return
        else:
            raise
    if ephemeral:
        cleanup.schedule(msg.chat.id, msg.message_id)
    else:
        cleanup.forget_menu(msg.chat.id, msg.message_id)


def menu_text(chat_row) -> str:
    n = db.count_events(chat_row["chat_id"])
    ntf = "✅ включены" if chat_row["enabled"] else "🔕 выключены"
    sched = reports.format_schedule(chat_row["days_mask"], chat_row["send_time"]) or "не задано"
    text = (
        "<b>Меню бота</b>\n\n"
        f"Уведомления: {ntf}\n"
        f"Часовой пояс: {config.tz_label(chat_row['tz'])}\n"
        f"Общее расписание: {sched}\n"
        f"Мероприятий в этом чате: {n}"
    )
    if chat_row["chat_id"] in cleanup.no_delete_rights:
        text += (
            "\n\n⚠️ Ваши команды остаются в чате: у меня нет права удалять "
            "чужие сообщения. Чтобы я убирал и их, сделайте меня "
            "администратором с правом «Удаление сообщений». Свои сообщения "
            "я убираю в любом случае."
        )
    return text


async def render_menu(cb: CallbackQuery) -> None:
    chat = db.get_chat(cb.message.chat.id)
    await safe_edit(
        cb.message, menu_text(chat), keyboards.kb_main(chat, db.count_events(chat["chat_id"]))
    )


async def run_report(bot: Bot, chat_id: int) -> None:
    chat = db.get_chat(chat_id)
    events = db.get_events(chat_id)
    note = await bot.send_message(chat_id, "⏳ Собираю данные из QTickets…")
    try:
        text = await reports.build_report(qtickets.get_client(), chat, events)
    except Exception as e:
        log.exception("Сбой отчёта в чате %s", chat_id)
        text = f"⚠️ Не получилось собрать отчёт: {html.escape(str(e))}"
    # Отчёт — это не служебное сообщение, он остаётся в чате.
    await safe_edit(note, text, ephemeral=False)


async def _drop(message: Message) -> None:
    """Убрать сообщение собеседника — команду или ответ на вопрос бота.

    В личной переписке Телеграм это разрешает всегда. В группе — только если
    бот администратор с правом «Удаление сообщений»; если права нет,
    сообщение просто останется. Запоминаем это, чтобы подсказать в меню.
    """
    chat_id = message.chat.id
    ok = await cleanup.delete_now(message.bot, chat_id, message.message_id)
    if message.chat.type == "private":
        return
    if ok:
        cleanup.no_delete_rights.discard(chat_id)
    else:
        cleanup.no_delete_rights.add(chat_id)


def _chat_title(message: Message) -> str:
    if message.chat.title:
        return message.chat.title
    if message.from_user:
        return message.from_user.full_name
    return "Чат"


# ---------- Доступ: кнопки может нажимать только владелец ----------

class OwnerCallbackMiddleware(BaseMiddleware):
    async def __call__(self, handler, event: CallbackQuery, data):
        if not config.is_owner(event.from_user.id):
            await event.answer("Эта кнопка доступна только владельцу бота.", show_alert=True)
            return None
        if event.message is None:
            await event.answer("Сообщение устарело. Откройте меню заново: /menu", show_alert=True)
            return None
        return await handler(event, data)


router.callback_query.middleware(OwnerCallbackMiddleware())


# ---------- Добавление и удаление бота из чатов ----------

def _is_member(m) -> bool:
    return m.status in ("member", "administrator") or (
        m.status == "restricted" and bool(getattr(m, "is_member", False))
    )


@router.my_chat_member()
async def on_membership(update: ChatMemberUpdated, bot: Bot):
    was = _is_member(update.old_chat_member)
    now = _is_member(update.new_chat_member)
    chat = update.chat

    if now and not was:
        if chat.type == "channel":
            try:
                await bot.send_message(
                    chat.id, "Каналы я не поддерживаю — добавьте меня в группу."
                )
            except Exception:
                pass
            await bot.leave_chat(chat.id)
            return
        actor = update.from_user
        if actor is None or not config.is_owner(actor.id):
            log.info("Бота добавил посторонний (%s) в чат %s — выхожу", actor, chat.id)
            try:
                await bot.send_message(
                    chat.id, "Это личный бот, он работает только для своего владельца."
                )
            except Exception:
                pass
            try:
                await bot.leave_chat(chat.id)
            except Exception:
                pass
            db_row = None
            try:
                db_row = db.get_chat(chat.id)
            finally:
                if db_row is not None:
                    db.set_enabled(chat.id, 0)
            return
        db.get_chat(chat.id, chat.title or "Чат")
        try:
            hello = await bot.send_message(
                chat.id,
                "Привет! Я буду присылать сюда отчёты о купленных билетах.\n"
                "Откройте меню: /menu",
            )
            cleanup.schedule(chat.id, hello.message_id, cleanup.NOTICE_LIFETIME)
        except Exception:
            pass
        return

    if was and not now:
        # Бота убрали из чата: настройки не стираем, только выключаем уведомления,
        # чтобы при возврате бота всё осталось на месте.
        db.set_enabled(chat.id, 0)


@router.message(F.migrate_to_chat_id)
async def on_migrate(message: Message):
    # Телеграм превратил группу в супергруппу — переносим настройки на новый ID чата
    db.migrate_chat(message.chat.id, message.migrate_to_chat_id)
    log.info("Чат %s переехал на %s", message.chat.id, message.migrate_to_chat_id)


# ---------- Команды ----------

@router.message(Command("start", "menu"))
async def cmd_menu(message: Message, state: FSMContext):
    if message.chat.type == "channel":
        return
    user_id = message.from_user.id if message.from_user else None
    if not config.is_owner(user_id):
        if message.chat.type == "private":
            await message.answer("Это личный бот, доступ к нему ограничен.")
        return
    await _drop(message)
    current = await state.get_state()
    if current:
        await message.answer(
            confirm_flow_text(current), reply_markup=keyboards.kb_flow_confirm()
        )
        return
    chat = db.get_chat(message.chat.id, _chat_title(message))
    sent = await message.answer(
        menu_text(chat), reply_markup=keyboards.kb_main(chat, db.count_events(chat["chat_id"]))
    )
    await cleanup.register_menu(message.bot, message.chat.id, sent.message_id)


@router.message(Command("report"))
async def cmd_report(message: Message):
    user_id = message.from_user.id if message.from_user else None
    if not config.is_owner(user_id):
        if message.chat.type == "private":
            await message.answer("Это личный бот, доступ к нему ограничен.")
        return
    await _drop(message)
    db.get_chat(message.chat.id, _chat_title(message))
    await run_report(message.bot, message.chat.id)


@router.message(Command("help"))
async def cmd_help(message: Message):
    if not config.is_owner(message.from_user.id if message.from_user else None):
        return
    await _drop(message)
    help_msg = await message.answer(HELP_TEXT)
    cleanup.schedule(message.chat.id, help_msg.message_id, cleanup.MENU_LIFETIME)


# ---------- Ввод ID мероприятия вручную ----------

@router.message(AddEventState.waiting_id)
async def got_event_id(message: Message, state: FSMContext):
    if not config.is_owner(message.from_user.id if message.from_user else None):
        return
    data = await state.get_data()
    prompt_id = data.get("prompt_id")
    in_group = message.chat.type != "private"
    if not _is_reply_to_prompt(message, prompt_id):
        return

    text = (message.text or "").strip()
    if text.startswith("/"):
        await message.reply(
            "Сейчас я жду ID мероприятия. Чтобы выйти в меню — отправьте /menu."
        )
        return
    if not text.isdigit():
        hint = " (ответом на моё сообщение выше)" if in_group else ""
        await message.reply(
            f"Нужно число — ID мероприятия из QTickets. Попробуйте ещё раз{hint}."
        )
        return

    event_id = int(text)
    note = await message.reply("⏳ Проверяю мероприятие в QTickets…")
    try:
        info = await qtickets.get_client().get_event(event_id)
    except QTicketsError as e:
        hint = " ответом на моё первое сообщение" if in_group else ""
        await safe_edit(
            note,
            f"⚠️ {html.escape(str(e))}\nПроверьте ID и отправьте ещё раз{hint}.",
        )
        return
    except Exception:
        log.exception("Сбой проверки мероприятия %s", event_id)
        await safe_edit(note, "⚠️ Внутренняя ошибка. Попробуйте ещё раз позже.")
        return

    await state.clear()
    if prompt_id:
        await cleanup.delete_now(message.bot, message.chat.id, prompt_id)
    await _drop(message)
    added = db.add_event(message.chat.id, event_id, info["name"])
    if added:
        await safe_edit(
            note,
            f"✅ Добавлено: «{html.escape(info['name'])}».\n"
            "Пока для него действует общее расписание чата.",
            keyboards.kb_after_add(event_id),
        )
    else:
        await safe_edit(
            note,
            f"Мероприятие «{html.escape(info['name'])}» уже добавлено в этот чат.",
            keyboards.kb_back_menu(),
        )


# ---------- Ввод расписания текстом ----------

async def _ask(message: Message, text: str, placeholder: str, user=None) -> int:
    """Задать вопрос с полем для ответа. Вернуть номер своего сообщения.

    Тонкость Телеграма: если у поля ответа стоит пометка «только для
    определённых людей» (selective), оно показывается лишь тем, кто упомянут
    в тексте сообщения. Без упоминания поле не покажется НИКОМУ — из-за этого
    вопрос выглядел как «кнопка не работает». Поэтому:
    в личной переписке пометку не ставим вовсе, а в группе ставим только
    вместе с упоминанием собеседника.
    """
    prefix = ""
    selective = None
    if message.chat.type != "private" and user is not None:
        if user.username:
            prefix = f"@{user.username}, "
            selective = True
        else:
            prefix = f'<a href="tg://user?id={user.id}">{html.escape(user.full_name)}</a>, '

    prompt = await message.answer(
        prefix + text,
        reply_markup=ForceReply(
            selective=selective, input_field_placeholder=placeholder
        ),
    )
    cleanup.schedule(message.chat.id, prompt.message_id, cleanup.MENU_LIFETIME)
    return prompt.message_id


def _is_reply_to_prompt(message: Message, prompt_id) -> bool:
    """В группе принимаем ответ на наше сообщение — так вопрос работает
    даже при включённом «режиме конфиденциальности» бота."""
    if message.chat.type == "private":
        return True
    reply = message.reply_to_message
    if reply is None:
        # Обычное сообщение в группе бот получает только если у него выключен
        # «режим конфиденциальности». Раз уж получили — принимаем: вопрос задан,
        # спрашивающий проверен, ждать именно ответа-цитаты незачем.
        return True
    if reply.message_id == prompt_id:
        return True
    # Ответ на любое другое наше сообщение тоже подходит: приглашение могло
    # исчезнуть и быть задано заново.
    return bool(reply.from_user and reply.from_user.is_bot)


@router.message(ScheduleInput.waiting_time)
async def got_time_text(message: Message, state: FSMContext):
    if not config.is_owner(message.from_user.id if message.from_user else None):
        return
    data = await state.get_data()
    if not _is_reply_to_prompt(message, data.get("prompt_id")):
        return

    text = (message.text or "").strip()
    if text.startswith("/"):
        await message.reply("Сейчас я жду время. Чтобы выйти в меню — отправьте /menu.")
        return

    send_time = reports.parse_time(text)
    if send_time is None:
        await message.reply(
            "Не разобрал время. Напишите часы и минуты, например: "
            "<code>18:32</code> или <code>9:05</code>."
        )
        return

    scope = str(data.get("scope", "c"))
    mask = int(data.get("mask") or 0)
    if mask <= 0:
        await state.clear()
        await message.reply("Что-то пошло не так — начните заново из меню: /menu")
        return

    chat_id = message.chat.id
    db.get_chat(chat_id, _chat_title(message))
    if scope == "c":
        db.set_chat_schedule(chat_id, mask, send_time)
        label = "общее для чата"
    else:
        ev = db.get_event(chat_id, int(scope))
        if ev is None:
            await state.clear()
            await message.reply("Этого мероприятия уже нет в чате. Откройте меню: /menu")
            return
        db.set_event_schedule(chat_id, int(scope), mask, send_time)
        label = f"для «{html.escape(ev['name'] or scope)}»"

    if data.get("prompt_id"):
        await cleanup.delete_now(message.bot, chat_id, data["prompt_id"])
    await _drop(message)
    await state.clear()

    done = await message.answer(
        f"✅ Расписание сохранено ({label}):\n"
        f"<b>{reports.format_days(mask)} в {send_time}</b>",
        reply_markup=keyboards.kb_after_schedule(scope),
    )
    cleanup.schedule(chat_id, done.message_id, cleanup.MENU_LIFETIME)


@router.message(F.chat.type == "private", F.text, ~F.text.startswith("/"))
async def private_text(message: Message, state: FSMContext):
    if not config.is_owner(message.from_user.id if message.from_user else None):
        await message.answer("Это личный бот, доступ к нему ограничен.")
        return
    if (message.text or "").strip().lower() in ("старт", "меню", "start", "menu"):
        chat = db.get_chat(message.chat.id, _chat_title(message))
        sent = await message.answer(
            menu_text(chat),
            reply_markup=keyboards.kb_main(chat, db.count_events(chat["chat_id"])),
        )
        await cleanup.register_menu(message.bot, message.chat.id, sent.message_id)
        return
    hint = await message.answer("Откройте меню командой /menu")
    cleanup.schedule(message.chat.id, hint.message_id, cleanup.NOTICE_LIFETIME)


# ---------- Кнопки: базовые ----------

@router.callback_query(F.data == "m")
async def cb_menu(cb: CallbackQuery):
    await cb.answer()
    await render_menu(cb)


@router.callback_query(F.data == "x")
async def cb_close(cb: CallbackQuery):
    await cb.answer("Меню закрыто")
    await cleanup.delete_now(cb.bot, cb.message.chat.id, cb.message.message_id)
    cleanup.forget_menu(cb.message.chat.id, cb.message.message_id)


@router.callback_query(F.data == "noop")
async def cb_noop(cb: CallbackQuery):
    await cb.answer()


@router.callback_query(F.data == "rep")
async def cb_report(cb: CallbackQuery):
    await cb.answer("Собираю отчёт…")
    await run_report(cb.bot, cb.message.chat.id)


@router.callback_query(F.data == "ntf")
async def cb_notifications(cb: CallbackQuery):
    chat = db.get_chat(cb.message.chat.id)
    new_value = 0 if chat["enabled"] else 1
    db.set_enabled(chat["chat_id"], new_value)
    await cb.answer("Уведомления включены" if new_value else "Уведомления выключены")
    await render_menu(cb)


# ---------- Кнопки: часовой пояс ----------

@router.callback_query(F.data == "tz")
async def cb_tz(cb: CallbackQuery):
    await cb.answer()
    chat = db.get_chat(cb.message.chat.id)
    await safe_edit(
        cb.message,
        "🌍 <b>Часовой пояс этого чата</b>\n\n"
        "По нему бот понимает, что такое «сегодня», и во сколько отправлять "
        "уведомления. Выберите пояс города, где идут показы:",
        keyboards.kb_timezones(chat["tz"]),
    )


def _retime_schedules(chat_id: int, old_tz: str, new_tz: str) -> list:
    """Перевести все расписания чата в новый часовой пояс.

    Момент отправки сохраняется: 13:05 по Москве становится 15:05 по
    Екатеринбургу. Заодно помечаем в журнале уже отправленные сегодня отчёты
    под новым временем, чтобы смена пояса не вызвала повторное уведомление.
    """
    if old_tz == new_tz:
        return []

    changes = []
    chat = db.get_chat(chat_id)
    new_mask, new_time = reports.shift_schedule(
        chat["days_mask"], chat["send_time"], old_tz, new_tz
    )
    if new_time and new_time != chat["send_time"]:
        db.set_chat_schedule(chat_id, new_mask, new_time)
        changes.append(
            (
                "общее расписание чата",
                reports.format_schedule(chat["days_mask"], chat["send_time"]),
                reports.format_schedule(new_mask, new_time),
            )
        )

    try:
        today_new = datetime.now(ZoneInfo(new_tz)).date().isoformat()
        today_old = datetime.now(ZoneInfo(old_tz)).date().isoformat()
    except Exception:
        today_new = today_old = None

    for ev in db.get_events(chat_id):
        if ev["days_mask"] and ev["send_time"]:
            ev_mask, ev_time = reports.shift_schedule(
                ev["days_mask"], ev["send_time"], old_tz, new_tz
            )
            if ev_time and ev_time != ev["send_time"]:
                db.set_event_schedule(chat_id, ev["event_id"], ev_mask, ev_time)
                name = ev["name"] or f"мероприятие {ev['event_id']}"
                changes.append(
                    (
                        f"«{html.escape(name)}»",
                        reports.format_schedule(ev["days_mask"], ev["send_time"]),
                        reports.format_schedule(ev_mask, ev_time),
                    )
                )
        # Защита от повторного уведомления в день смены пояса
        if today_new and today_old:
            already = db.sent_any_today(chat_id, ev["event_id"], today_old) or (
                today_new != today_old
                and db.sent_any_today(chat_id, ev["event_id"], today_new)
            )
            if already:
                fresh = db.get_event(chat_id, ev["event_id"])
                mask, send_time, _own = reports.effective_schedule(
                    fresh, db.get_chat(chat_id)
                )
                if send_time:
                    db.sent_add(chat_id, ev["event_id"], today_new, send_time)

    return changes


@router.callback_query(F.data.startswith("tzs:"))
async def cb_tz_set(cb: CallbackQuery):
    try:
        index = int(cb.data.split(":")[1])
        tz_name, tz_lbl = config.TIMEZONES[index]
    except (ValueError, IndexError):
        await cb.answer("Кнопка устарела. Откройте меню заново: /menu", show_alert=True)
        return
    chat_id = cb.message.chat.id
    chat = db.get_chat(chat_id)
    old_tz = chat["tz"] or config.DEFAULT_TZ
    changes = _retime_schedules(chat_id, old_tz, tz_name)
    db.set_tz(chat_id, tz_name)

    if changes:
        await cb.answer(f"Сохранено: {tz_lbl}")
        lines = "\n".join(
            f"• {what}: <b>{was}</b> → <b>{now}</b>" for what, was, now in changes
        )
        await safe_edit(
            cb.message,
            f"🌍 Часовой пояс чата: <b>{html.escape(tz_lbl)}</b>\n\n"
            "Время уведомлений пересчитано, чтобы отчёты приходили в тот же "
            "момент, что и раньше:\n"
            f"{lines}",
            keyboards.kb_back_menu(),
        )
    else:
        await cb.answer(f"Сохранено: {tz_lbl}")
        await render_menu(cb)


# ---------- Кнопки: список мероприятий чата ----------

async def render_events_list(cb: CallbackQuery, page: int) -> None:
    events = db.get_events(cb.message.chat.id)
    max_page = max(0, (len(events) - 1) // keyboards.EVENTS_PER_PAGE)
    page = min(max(page, 0), max_page)
    if events:
        text = (
            "🎭 <b>Мероприятия этого чата</b>\n\n"
            "Нажмите на мероприятие, чтобы настроить или удалить его."
        )
    else:
        text = (
            "🎭 <b>Мероприятия этого чата</b>\n\n"
            "Пока пусто. Нажмите «Добавить мероприятие»."
        )
    await safe_edit(cb.message, text, keyboards.kb_events_list(events, page))


@router.callback_query(F.data.startswith("evl:"))
async def cb_events_list(cb: CallbackQuery):
    await cb.answer()
    try:
        page = int(cb.data.split(":")[1])
    except (ValueError, IndexError):
        page = 0
    await render_events_list(cb, page)


async def render_event_card(cb: CallbackQuery, event_id: int) -> None:
    ev = db.get_event(cb.message.chat.id, event_id)
    if ev is None:
        await cb.answer("Этого мероприятия уже нет в чате.", show_alert=True)
        await render_events_list(cb, 0)
        return
    chat = db.get_chat(cb.message.chat.id)
    mask, send_time, own = reports.effective_schedule(ev, chat)
    sched = reports.format_schedule(mask, send_time)
    if sched and own:
        sched_line = f"своё: {sched}"
    elif sched:
        sched_line = f"общее для чата: {sched}"
    else:
        sched_line = "не задано — задайте своё или общее расписание"
    name = ev["name"] or f"Мероприятие {event_id}"
    text = (
        f"🎭 <b>«{html.escape(name)}»</b>\n"
        f"ID в QTickets: <code>{event_id}</code>\n\n"
        f"Расписание уведомлений: {sched_line}"
    )
    await safe_edit(cb.message, text, keyboards.kb_event_card(ev, own))


@router.callback_query(F.data.startswith("evc:"))
async def cb_event_card(cb: CallbackQuery):
    await cb.answer()
    try:
        event_id = int(cb.data.split(":")[1])
    except (ValueError, IndexError):
        await render_events_list(cb, 0)
        return
    await render_event_card(cb, event_id)


@router.callback_query(F.data.startswith("evdc:"))
async def cb_event_delete_confirmed(cb: CallbackQuery):
    try:
        event_id = int(cb.data.split(":")[1])
    except (ValueError, IndexError):
        await cb.answer()
        return
    ev = db.get_event(cb.message.chat.id, event_id)
    db.remove_event(cb.message.chat.id, event_id)
    name = (ev["name"] if ev else None) or f"Мероприятие {event_id}"
    await cb.answer(f"«{name}» удалено из чата")
    await render_events_list(cb, 0)


@router.callback_query(F.data.startswith("evd:"))
async def cb_event_delete_ask(cb: CallbackQuery):
    await cb.answer()
    try:
        event_id = int(cb.data.split(":")[1])
    except (ValueError, IndexError):
        return
    ev = db.get_event(cb.message.chat.id, event_id)
    if ev is None:
        await render_events_list(cb, 0)
        return
    name = ev["name"] or f"Мероприятие {event_id}"
    await safe_edit(
        cb.message,
        f"Удалить «{html.escape(name)}» из этого чата?\n"
        "Авто-уведомления по нему больше приходить не будут.",
        keyboards.kb_delete_confirm(event_id),
    )


# ---------- Кнопки: добавление мероприятия ----------

@router.callback_query(F.data.startswith("add:"))
async def cb_add_list(cb: CallbackQuery):
    await cb.answer()
    try:
        page = int(cb.data.split(":")[1])
    except (ValueError, IndexError):
        page = 0
    await safe_edit(cb.message, "⏳ Загружаю список мероприятий из QTickets…")
    try:
        items = await qtickets.get_client().list_events()
    except QTicketsError as e:
        await safe_edit(
            cb.message,
            f"⚠️ {html.escape(str(e))}\n\nМожно добавить мероприятие по ID вручную.",
            keyboards.kb_add_fallback(),
        )
        return
    if not items:
        await safe_edit(
            cb.message,
            "В QTickets не нашлось ни одного мероприятия.\n"
            "Можно добавить мероприятие по ID вручную.",
            keyboards.kb_add_fallback(),
        )
        return
    added_ids = {ev["event_id"] for ev in db.get_events(cb.message.chat.id)}
    max_page = max(0, (len(items) - 1) // keyboards.EVENTS_PER_PAGE)
    page = min(max(page, 0), max_page)
    await safe_edit(
        cb.message,
        "➕ <b>Добавить мероприятие</b>\n\n"
        "Выберите мероприятие из вашего QTickets (новые — сверху).\n"
        "✅ — уже добавлено в этот чат.",
        keyboards.kb_add_list(items, added_ids, page),
    )


@router.callback_query(F.data.startswith("pick:"))
async def cb_pick(cb: CallbackQuery):
    try:
        event_id = int(cb.data.split(":")[1])
    except (ValueError, IndexError):
        await cb.answer()
        return
    if db.get_event(cb.message.chat.id, event_id):
        await cb.answer("Уже добавлено в этот чат.", show_alert=True)
        return
    await cb.answer()
    await safe_edit(cb.message, "⏳ Проверяю мероприятие в QTickets…")
    try:
        info = await qtickets.get_client().get_event(event_id)
    except QTicketsError as e:
        await safe_edit(
            cb.message,
            f"⚠️ {html.escape(str(e))}",
            keyboards.kb_add_fallback(),
        )
        return
    db.add_event(cb.message.chat.id, event_id, info["name"])
    await safe_edit(
        cb.message,
        f"✅ Добавлено: «{html.escape(info['name'])}».\n"
        "Пока для него действует общее расписание чата.",
        keyboards.kb_after_add(event_id),
    )


@router.callback_query(F.data == "man")
async def cb_manual(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    prompt_id = await _ask(
        cb.message,
        "🔢 <b>Отправьте ID мероприятия из QTickets</b> — ответом на это сообщение.\n\n"
        "Это число из адресной строки страницы мероприятия в личном кабинете, "
        "например <code>12345</code>.",
        "ID мероприятия",
        user=cb.from_user,
    )
    await state.set_state(AddEventState.waiting_id)
    await state.update_data(prompt_id=prompt_id)


# ---------- Кнопки: настройка расписания ----------

def _scope_event(cb: CallbackQuery, scope: str):
    """Для scope-мероприятия вернуть его строку из базы (или None, если удалено)."""
    if scope == "c":
        return "c", None
    try:
        event_id = int(scope)
    except ValueError:
        return None, None
    return scope, db.get_event(cb.message.chat.id, event_id)


async def render_days(cb: CallbackQuery, scope: str, mask: int) -> None:
    """Экран выбора дней недели."""
    chat = db.get_chat(cb.message.chat.id)
    if scope == "c":
        title = "Общее расписание чата"
    else:
        _, ev = _scope_event(cb, scope)
        if ev is None:
            await cb.answer("Этого мероприятия уже нет в чате.", show_alert=True)
            await render_menu(cb)
            return
        name = ev["name"] or f"Мероприятие {scope}"
        title = f"Расписание «{html.escape(name)}»"
    await safe_edit(
        cb.message,
        f"⏰ <b>{title}</b>\n\n"
        "Отметьте дни недели, когда присылать отчёт, и нажмите «Далее».\n\n"
        f"Выбрано: <b>{reports.format_days(mask)}</b>\n"
        f"Пояс чата: {config.tz_label(chat['tz'])}",
        keyboards.kb_days(scope, mask),
    )


def _current_mask(cb: CallbackQuery, scope: str) -> int:
    """С чего начинать отметки: с того, что уже задано."""
    if scope == "c":
        return db.get_chat(cb.message.chat.id)["days_mask"] or 0
    _, ev = _scope_event(cb, scope)
    return (ev["days_mask"] or 0) if ev is not None else 0


@router.callback_query(F.data.startswith("sdt:"))
async def cb_day_toggle(cb: CallbackQuery):
    await cb.answer()
    try:
        _, scope, mask, day = cb.data.split(":")
        mask, day = int(mask), int(day)
    except (ValueError, IndexError):
        await render_menu(cb)
        return
    await render_days(cb, scope, mask ^ (1 << day))


@router.callback_query(F.data.startswith("sda:"))
async def cb_day_all(cb: CallbackQuery):
    await cb.answer()
    try:
        _, scope, mask = cb.data.split(":")
        mask = int(mask)
    except (ValueError, IndexError):
        await render_menu(cb)
        return
    await render_days(cb, scope, 0 if mask >= 127 else 127)


@router.callback_query(F.data.startswith("sd:"))
async def cb_days(cb: CallbackQuery):
    await cb.answer()
    parts = cb.data.split(":")
    scope = parts[1] if len(parts) > 1 else "c"
    await render_days(cb, scope, _current_mask(cb, scope))


@router.callback_query(F.data.startswith("sh:"))
async def cb_ask_time(cb: CallbackQuery, state: FSMContext):
    """Дни выбраны — спрашиваем время одним вопросом, без сеток часов и минут."""
    try:
        _, scope, mask = cb.data.split(":")
        mask = int(mask)
    except (ValueError, IndexError):
        await cb.answer()
        await render_menu(cb)
        return
    if mask <= 0:
        await cb.answer("Сначала отметьте хотя бы один день.", show_alert=True)
        return
    await cb.answer()
    chat = db.get_chat(cb.message.chat.id)
    prompt_id = await _ask(
        cb.message,
        TIME_PROMPT.format(
            days=reports.format_days(mask), tz=config.tz_label(chat["tz"])
        ),
        "18:32",
        user=cb.from_user,
    )
    await state.set_state(ScheduleInput.waiting_time)
    await state.update_data(scope=scope, mask=mask, prompt_id=prompt_id)


@router.callback_query(F.data.startswith("scl"))
async def cb_clear_schedule(cb: CallbackQuery):
    parts = cb.data.split(":")
    scope = parts[1] if len(parts) > 1 else "c"
    chat_id = cb.message.chat.id
    db.get_chat(chat_id)
    if scope == "c":
        db.set_chat_schedule(chat_id, None, None)
        await cb.answer("Общее расписание очищено")
        await safe_edit(
            cb.message,
            "Общее расписание очищено.\n"
            "Авто-уведомления теперь приходят только по мероприятиям, "
            "у которых задано своё расписание.",
            keyboards.kb_back_menu(),
        )
        return
    _, ev = _scope_event(cb, scope)
    if ev is None:
        await cb.answer()
        await render_menu(cb)
        return
    db.set_event_schedule(chat_id, int(scope), None, None)
    name = ev["name"] or f"Мероприятие {scope}"
    await cb.answer("Готово")
    await safe_edit(
        cb.message,
        f"«{html.escape(name)}» снова использует общее расписание чата.",
        keyboards.kb_after_schedule(scope),
    )


# ---------- Кнопки: подтверждение выхода из текущего действия ----------

@router.callback_query(F.data == "fc")
async def cb_flow_cancel(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    await state.clear()
    if data.get("prompt_id"):
        await cleanup.delete_now(cb.bot, cb.message.chat.id, data["prompt_id"])
    await cb.answer()
    await render_menu(cb)


@router.callback_query(F.data == "fs")
async def cb_flow_stay(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    what = _WAITING_HINTS.get(await state.get_state() or "", "завершите текущее действие.")
    await safe_edit(
        cb.message,
        f"Хорошо, продолжаем. Сейчас нужно: {what}\n"
        "Ответьте на моё сообщение выше.",
    )


@router.callback_query()
async def cb_unknown(cb: CallbackQuery):
    await cb.answer("Кнопка устарела. Откройте меню заново: /menu", show_alert=True)
