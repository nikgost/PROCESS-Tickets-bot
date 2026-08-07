#!/usr/bin/env bash
# Первичная настройка бота: создаёт файл .env с ключами доступа.
# Запуск из папки бота:  bash setup.sh
#
# Ключи вводятся вручную и НЕ попадают в историю команд Терминала —
# в отличие от случая, когда их вписывают прямо в командную строку.

set -u

cd "$(dirname "$0")" || exit 1

echo "Настройка бота отчётов о билетах."
echo "Нужно ввести три значения. Ключи не сохраняются в истории команд."
echo

if [ -f .env ]; then
  echo "Файл .env уже есть."
  printf "Перезаписать его? (да/нет): "
  read -r answer
  case "$answer" in
    да | Да | ДА | y | Y | yes | Yes | д | Д) echo ;;
    *)
      echo "Отменено, файл .env не тронут."
      exit 0
      ;;
  esac
fi

# ask ПОДСКАЗКА РЕГУЛЯРНОЕ_ВЫРАЖЕНИЕ ТЕКСТ_ОШИБКИ -> результат в переменной REPLY_VALUE
ask() {
  local prompt="$1" pattern="$2" error="$3" value=""
  while true; do
    printf "%s" "$prompt"
    read -r value
    value="${value#"${value%%[![:space:]]*}"}" # убрать пробелы слева
    value="${value%"${value##*[![:space:]]}"}" # убрать пробелы справа
    if [ -z "$value" ]; then
      echo "  ↳ Пусто. Попробуйте ещё раз."
      continue
    fi
    if [[ "$value" =~ $pattern ]]; then
      REPLY_VALUE="$value"
      return 0
    fi
    echo "  ↳ $error"
  done
}

echo "1) Токен телеграм-бота — его выдаёт @BotFather."
echo "   Выглядит так: 1234567890:AAH... (цифры, двоеточие, длинный хвост)"
ask "   Вставьте токен бота: " '^[0-9]+:[A-Za-z0-9_-]{20,}$' \
  "Не похоже на токен бота. Должно быть: цифры, двоеточие, длинный хвост."
BOT_TOKEN="$REPLY_VALUE"
echo

echo "2) Токен QTickets — личный кабинет, «Настройки → Основное», в самом низу."
ask "   Вставьте токен QTickets: " '^[A-Za-z0-9_.:-]{10,}$' \
  "Не похоже на ключ QTickets (слишком коротко или есть пробелы)."
QTICKETS_TOKEN="$REPLY_VALUE"
echo

echo "3) Ваш числовой Телеграм-ID — узнать можно у бота @userinfobot."
echo "   Если управлять ботом будут несколько человек — их ID через запятую."
ask "   Введите ID: " '^[0-9]+([[:space:]]*,[[:space:]]*[0-9]+)*$' \
  "Нужны только цифры (несколько ID — через запятую)."
OWNER_IDS="${REPLY_VALUE// /}"
echo

umask 077
cat > .env << EOF
# Файл создан скриптом setup.sh. Ключи доступа — не пересылайте этот файл.
BOT_TOKEN=$BOT_TOKEN
QTICKETS_TOKEN=$QTICKETS_TOKEN
OWNER_IDS=$OWNER_IDS
EOF
chmod 600 .env

mask() { # показать только края значения, середину скрыть
  local v="$1"
  if [ "${#v}" -le 10 ]; then
    echo "***"
  else
    echo "${v:0:4}…${v: -4}"
  fi
}

echo "Готово, файл .env создан:"
echo "  BOT_TOKEN      = $(mask "$BOT_TOKEN")"
echo "  QTICKETS_TOKEN = $(mask "$QTICKETS_TOKEN")"
echo "  OWNER_IDS      = $OWNER_IDS"
echo
echo "Проверить запуск:   venv/bin/python main.py"
echo "Изменить ключи потом: nano .env   (или снова bash setup.sh)"
