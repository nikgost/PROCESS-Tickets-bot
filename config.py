"""Настройки бота. Значения берутся из файла .env, лежащего рядом с кодом."""

import os

from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
QTICKETS_TOKEN = os.environ.get("QTICKETS_TOKEN", "").strip()
QTICKETS_BASE_URL = os.environ.get(
    "QTICKETS_BASE_URL", "https://qtickets.ru/api/rest/v1"
).strip()
DB_PATH = os.environ.get("DB_PATH", "data.db").strip()


def _parse_ids(raw: str) -> set[int]:
    ids: set[int] = set()
    for part in raw.replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            ids.add(int(part))
        except ValueError:
            pass
    return ids


# Телеграм-ID людей, которым разрешено управлять ботом (через запятую в .env)
OWNER_IDS: set[int] = _parse_ids(os.environ.get("OWNER_IDS", ""))


def is_owner(user_id: int | None) -> bool:
    return user_id is not None and user_id in OWNER_IDS


# Часовые пояса России. По поясу чата бот понимает, что такое «сегодня»
# и во сколько отправлять уведомления.
TIMEZONES: list[tuple[str, str]] = [
    ("Europe/Kaliningrad", "Калининград (МСК−1)"),
    ("Europe/Moscow", "Москва (МСК)"),
    ("Europe/Samara", "Самара (МСК+1)"),
    ("Asia/Yekaterinburg", "Екатеринбург, Пермь (МСК+2)"),
    ("Asia/Omsk", "Омск (МСК+3)"),
    ("Asia/Krasnoyarsk", "Красноярск, Новосибирск (МСК+4)"),
    ("Asia/Irkutsk", "Иркутск (МСК+5)"),
    ("Asia/Yakutsk", "Якутск (МСК+6)"),
    ("Asia/Vladivostok", "Владивосток (МСК+7)"),
    ("Asia/Magadan", "Магадан (МСК+8)"),
    ("Asia/Kamchatka", "Камчатка (МСК+9)"),
]

DEFAULT_TZ = "Europe/Moscow"


def tz_label(tz_name: str | None) -> str:
    for name, label in TIMEZONES:
        if name == tz_name:
            return label
    return tz_name or DEFAULT_TZ


def validate() -> list[str]:
    """Проверка настроек перед запуском. Возвращает список ошибок (пусто = всё в порядке)."""
    errors = []
    if not BOT_TOKEN:
        errors.append(
            "Не задан BOT_TOKEN — токен телеграм-бота. Получите его у @BotFather и впишите в файл .env"
        )
    if not QTICKETS_TOKEN:
        errors.append(
            "Не задан QTICKETS_TOKEN — ключ доступа к QTickets. Он формируется в личном кабинете: "
            "Настройки → Основное (в самом низу). Впишите его в файл .env"
        )
    if not OWNER_IDS:
        errors.append(
            "Не задан OWNER_IDS — ваш числовой Телеграм-ID (можно несколько через запятую). "
            "Узнать свой ID: напишите боту @userinfobot. Впишите в файл .env"
        )
    return errors
