"""Хранилище настроек бота — один файл SQLite (data.db) рядом с кодом.

Таблицы:
  chats       — чаты, куда добавлен бот, и их общие настройки
  chat_events — мероприятия, привязанные к чату (и их личные расписания)
  sent_log    — журнал уже отправленных уведомлений (защита от повторов)
"""

from __future__ import annotations  # чтобы код работал и на Python 3.9

import sqlite3
import threading
from datetime import date, timedelta

import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS chats (
    chat_id   INTEGER PRIMARY KEY,
    title     TEXT,
    enabled   INTEGER NOT NULL DEFAULT 1,
    tz        TEXT NOT NULL DEFAULT 'Europe/Moscow',
    days_mask INTEGER,      -- дни недели общего расписания (битовая маска, NULL = не задано)
    send_time TEXT          -- время общего расписания 'ЧЧ:ММ' (NULL = не задано)
);
CREATE TABLE IF NOT EXISTS chat_events (
    chat_id   INTEGER NOT NULL,
    event_id  INTEGER NOT NULL,
    name      TEXT,
    days_mask INTEGER,      -- своё расписание мероприятия (NULL = используется общее)
    send_time TEXT,
    PRIMARY KEY (chat_id, event_id)
);
CREATE TABLE IF NOT EXISTS sent_log (
    chat_id    INTEGER NOT NULL,
    event_id   INTEGER NOT NULL,
    local_date TEXT NOT NULL,   -- дата по поясу чата, ГГГГ-ММ-ДД
    hhmm       TEXT NOT NULL,
    PRIMARY KEY (chat_id, event_id, local_date, hhmm)
);
"""

_conn: sqlite3.Connection | None = None
_lock = threading.Lock()


def init(path: str | None = None) -> None:
    global _conn
    _conn = sqlite3.connect(path or config.DB_PATH, check_same_thread=False)
    _conn.row_factory = sqlite3.Row
    _conn.execute("PRAGMA journal_mode=WAL")
    _conn.executescript(SCHEMA)
    _conn.commit()


def _exec(sql: str, args: tuple = ()) -> sqlite3.Cursor:
    with _lock:
        cur = _conn.execute(sql, args)
        _conn.commit()
        return cur


def _q(sql: str, args: tuple = ()) -> list[sqlite3.Row]:
    with _lock:
        return _conn.execute(sql, args).fetchall()


def _q1(sql: str, args: tuple = ()) -> sqlite3.Row | None:
    with _lock:
        return _conn.execute(sql, args).fetchone()


# ---------- Чаты ----------

def get_chat(chat_id: int, title: str | None = None) -> sqlite3.Row:
    """Вернуть чат; если его ещё нет — создать с настройками по умолчанию."""
    row = _q1("SELECT * FROM chats WHERE chat_id=?", (chat_id,))
    if row is None:
        _exec(
            "INSERT INTO chats(chat_id, title, tz) VALUES(?,?,?)",
            (chat_id, title or "", config.DEFAULT_TZ),
        )
        row = _q1("SELECT * FROM chats WHERE chat_id=?", (chat_id,))
    elif title and row["title"] != title:
        _exec("UPDATE chats SET title=? WHERE chat_id=?", (title, chat_id))
        row = _q1("SELECT * FROM chats WHERE chat_id=?", (chat_id,))
    return row


def set_enabled(chat_id: int, enabled: int) -> None:
    _exec("UPDATE chats SET enabled=? WHERE chat_id=?", (1 if enabled else 0, chat_id))


def set_tz(chat_id: int, tz: str) -> None:
    _exec("UPDATE chats SET tz=? WHERE chat_id=?", (tz, chat_id))


def set_chat_schedule(chat_id: int, days_mask: int | None, send_time: str | None) -> None:
    _exec(
        "UPDATE chats SET days_mask=?, send_time=? WHERE chat_id=?",
        (days_mask, send_time, chat_id),
    )


def enabled_chats() -> list[sqlite3.Row]:
    return _q("SELECT * FROM chats WHERE enabled=1")


def migrate_chat(old_id: int, new_id: int) -> None:
    """Телеграм иногда превращает обычную группу в супергруппу — у чата меняется ID."""
    with _lock:
        _conn.execute("DELETE FROM chats WHERE chat_id=?", (new_id,))
        _conn.execute("DELETE FROM chat_events WHERE chat_id=?", (new_id,))
        _conn.execute("DELETE FROM sent_log WHERE chat_id=?", (new_id,))
        _conn.execute("UPDATE chats SET chat_id=? WHERE chat_id=?", (new_id, old_id))
        _conn.execute("UPDATE chat_events SET chat_id=? WHERE chat_id=?", (new_id, old_id))
        _conn.execute("UPDATE sent_log SET chat_id=? WHERE chat_id=?", (new_id, old_id))
        _conn.commit()


# ---------- Мероприятия чата ----------

def get_events(chat_id: int) -> list[sqlite3.Row]:
    return _q(
        "SELECT * FROM chat_events WHERE chat_id=? ORDER BY COALESCE(name,''), event_id",
        (chat_id,),
    )


def get_event(chat_id: int, event_id: int) -> sqlite3.Row | None:
    return _q1(
        "SELECT * FROM chat_events WHERE chat_id=? AND event_id=?", (chat_id, event_id)
    )


def count_events(chat_id: int) -> int:
    row = _q1("SELECT COUNT(*) AS n FROM chat_events WHERE chat_id=?", (chat_id,))
    return int(row["n"]) if row else 0


def add_event(chat_id: int, event_id: int, name: str) -> bool:
    """Добавить мероприятие в чат. Вернёт False, если оно уже было добавлено."""
    cur = _exec(
        "INSERT OR IGNORE INTO chat_events(chat_id, event_id, name) VALUES(?,?,?)",
        (chat_id, event_id, name),
    )
    return cur.rowcount > 0


def update_event_name(chat_id: int, event_id: int, name: str) -> None:
    _exec(
        "UPDATE chat_events SET name=? WHERE chat_id=? AND event_id=? AND COALESCE(name,'')<>?",
        (name, chat_id, event_id, name),
    )


def remove_event(chat_id: int, event_id: int) -> None:
    _exec("DELETE FROM chat_events WHERE chat_id=? AND event_id=?", (chat_id, event_id))
    _exec("DELETE FROM sent_log WHERE chat_id=? AND event_id=?", (chat_id, event_id))


def set_event_schedule(
    chat_id: int, event_id: int, days_mask: int | None, send_time: str | None
) -> None:
    _exec(
        "UPDATE chat_events SET days_mask=?, send_time=? WHERE chat_id=? AND event_id=?",
        (days_mask, send_time, chat_id, event_id),
    )


# ---------- Журнал отправок ----------

def sent_exists(chat_id: int, event_id: int, local_date: str, hhmm: str) -> bool:
    return (
        _q1(
            "SELECT 1 FROM sent_log WHERE chat_id=? AND event_id=? AND local_date=? AND hhmm=?",
            (chat_id, event_id, local_date, hhmm),
        )
        is not None
    )


def sent_add(chat_id: int, event_id: int, local_date: str, hhmm: str) -> None:
    _exec(
        "INSERT OR IGNORE INTO sent_log(chat_id, event_id, local_date, hhmm) VALUES(?,?,?,?)",
        (chat_id, event_id, local_date, hhmm),
    )


def sent_cleanup(days: int = 40) -> None:
    threshold = (date.today() - timedelta(days=days)).isoformat()
    _exec("DELETE FROM sent_log WHERE local_date < ?", (threshold,))
