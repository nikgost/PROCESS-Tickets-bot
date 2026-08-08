"""Кнопочные меню бота.

Каждая кнопка несёт короткую команду (callback data, не длиннее 64 знаков).
Всё нужное состояние закодировано прямо в командах кнопок, поэтому меню
продолжают работать даже после перезапуска бота.

Команды кнопок:
  m                       — главное меню
  rep                     — отчёт за сегодня прямо сейчас
  ntf                     — включить/выключить уведомления в чате
  tz / tzs:{i}            — выбор часового пояса
  evl:{стр}               — список мероприятий чата
  evc:{id}                — карточка мероприятия
  evd:{id} / evdc:{id}    — удаление мероприятия (вопрос / подтверждение)
  add:{стр}               — добавить мероприятие (список из QTickets)
  pick:{id}               — выбрать мероприятие из списка
  man                     — ввести ID мероприятия вручную
  sd:{scope}              — выбор дней (scope: c = общее для чата, число = ID мероприятия)
  sdt:{scope}:{mask}:{d}  — переключить день недели
  sda:{scope}:{mask}      — отметить все дни / снять все
  sh:{scope}:{mask}       — перейти к вводу времени (текстом)
  scl:{scope}             — очистить расписание (для мероприятия = вернуть общее)
  fc / fs                 — «в главное меню» / «остаться и доделать»
  x                       — закрыть меню (убрать его из чата)
"""

from __future__ import annotations  # чтобы код работал и на Python 3.9

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

import config
from reports import DAY_NAMES

EVENTS_PER_PAGE = 8


def _btn(text: str, cb: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=cb)


def _kb(rows: list[list[InlineKeyboardButton]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _cut(text: str, limit: int = 32) -> str:
    text = str(text)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def kb_main(chat_row, n_events: int) -> InlineKeyboardMarkup:
    ntf_text = "🔕 Выключить уведомления" if chat_row["enabled"] else "🔔 Включить уведомления"
    return _kb(
        [
            [_btn("📊 Отчёт за сегодня", "rep")],
            [_btn(f"🎭 Мероприятия ({n_events})", "evl:0")],
            [_btn("➕ Добавить мероприятие", "add:0")],
            [_btn("⏰ Общее расписание", "sd:c")],
            [_btn(ntf_text, "ntf")],
            [_btn("🌍 Часовой пояс", "tz")],
            [_btn("✖️ Закрыть", "x")],
        ]
    )


def kb_back_menu() -> InlineKeyboardMarkup:
    return _kb([[_btn("🏠 Меню", "m")]])


def kb_events_list(events: list, page: int) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    start = page * EVENTS_PER_PAGE
    for ev in events[start : start + EVENTS_PER_PAGE]:
        title = ev["name"] or f"Мероприятие {ev['event_id']}"
        rows.append([_btn(f"«{_cut(title)}»", f"evc:{ev['event_id']}")])

    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(_btn("⬅️", f"evl:{page - 1}"))
    if start + EVENTS_PER_PAGE < len(events):
        nav.append(_btn("➡️", f"evl:{page + 1}"))
    if nav:
        rows.append(nav)

    rows.append([_btn("➕ Добавить мероприятие", "add:0")])
    rows.append([_btn("🏠 Меню", "m")])
    return _kb(rows)


def kb_event_card(event_row, has_override: bool) -> InlineKeyboardMarkup:
    ev_id = event_row["event_id"]
    rows = [[_btn("⏰ Своё расписание", f"sd:{ev_id}")]]
    if has_override:
        rows.append([_btn("♻️ Вернуть общее расписание", f"scl:{ev_id}")])
    rows.append([_btn("🗑 Удалить из чата", f"evd:{ev_id}")])
    rows.append([_btn("⬅️ К списку", "evl:0")])
    rows.append([_btn("🏠 Меню", "m")])
    return _kb(rows)


def kb_delete_confirm(event_id: int) -> InlineKeyboardMarkup:
    return _kb(
        [
            [_btn("🗑 Да, удалить", f"evdc:{event_id}")],
            [_btn("Отмена", f"evc:{event_id}")],
        ]
    )


def kb_add_list(items: list, added_ids: set[int], page: int) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    start = page * EVENTS_PER_PAGE
    for it in items[start : start + EVENTS_PER_PAGE]:
        if it["id"] in added_ids:
            rows.append([_btn(f"✅ «{_cut(it['name'], 28)}»", f"evc:{it['id']}")])
        else:
            rows.append([_btn(f"«{_cut(it['name'])}»", f"pick:{it['id']}")])

    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(_btn("⬅️", f"add:{page - 1}"))
    if start + EVENTS_PER_PAGE < len(items):
        nav.append(_btn("➡️", f"add:{page + 1}"))
    if nav:
        rows.append(nav)

    rows.append([_btn("✍️ Ввести ID вручную", "man")])
    rows.append([_btn("🏠 Меню", "m")])
    return _kb(rows)


def kb_add_fallback() -> InlineKeyboardMarkup:
    return _kb(
        [
            [_btn("✍️ Ввести ID вручную", "man")],
            [_btn("🏠 Меню", "m")],
        ]
    )


def kb_after_add(event_id: int) -> InlineKeyboardMarkup:
    return _kb(
        [
            [_btn("⏰ Задать своё расписание", f"sd:{event_id}")],
            [_btn("🏠 Меню", "m")],
        ]
    )


# ---------- Настройка расписания ----------

def _back_cb(scope: str) -> str:
    return "m" if scope == "c" else f"evc:{scope}"


def kb_days(scope: str, mask: int) -> InlineKeyboardMarkup:
    """Отметки дней недели + переход к вводу времени."""
    day_buttons = []
    for i, name in enumerate(DAY_NAMES):
        mark = "✅ " if (mask >> i) & 1 else ""
        day_buttons.append(_btn(f"{mark}{name}", f"sdt:{scope}:{mask}:{i}"))
    rows = [day_buttons[:4], day_buttons[4:]]

    all_text = "Снять все дни" if mask >= 127 else "Каждый день"
    rows.append([_btn(all_text, f"sda:{scope}:{mask}")])
    rows.append([_btn("Далее: время →", f"sh:{scope}:{mask}")])
    if scope == "c":
        rows.append([_btn("🚫 Очистить общее расписание", "scl:c")])
    else:
        rows.append([_btn("♻️ Вернуть общее расписание", f"scl:{scope}")])
    rows.append([_btn("⬅️ Назад", _back_cb(scope))])
    return _kb(rows)


def kb_after_schedule(scope: str) -> InlineKeyboardMarkup:
    rows = []
    if scope != "c":
        rows.append([_btn("⬅️ К мероприятию", f"evc:{scope}")])
    rows.append([_btn("🏠 Меню", "m")])
    return _kb(rows)


def kb_timezones(current_tz: str | None) -> InlineKeyboardMarkup:
    rows = []
    for i, (name, label) in enumerate(config.TIMEZONES):
        mark = "✅ " if name == current_tz else ""
        rows.append([_btn(f"{mark}{label}", f"tzs:{i}")])
    rows.append([_btn("⬅️ Назад", "m")])
    return _kb(rows)


def kb_flow_confirm() -> InlineKeyboardMarkup:
    return _kb(
        [
            [_btn("Хочу в главное меню", "fc")],
            [_btn("Хочу остаться и доделать", "fs")],
        ]
    )
