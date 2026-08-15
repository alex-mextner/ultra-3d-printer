#!/usr/bin/env bash
# Универсальная утилита для чтения/записи БАЗЫ MOONRAKER (moonraker.db, SQLite
# на принтере) по HTTP API -- без похода в веб-интерфейс руками.
#
# ЗАЧЕМ ЭТО ОТДЕЛЬНО ОТ printer-configs/ И deploy.sh:
# Часть настроек (Mainsail UI: имя принтера, раскладка дашборда, тема,
# ПРЕСЕТЫ ТЕМПЕРАТУР и т.п.) живёт не в printer.cfg/moonraker.conf, а в этой
# базе -- отдельный слой хранения, см. CLAUDE.md, "Настройки Mainsail UI".
# scripts/export-mainsail-ui.sh уже читает её (в одну сторону, для снимка в
# git) -- этот скрипт добавляет ЗАПИСЬ, и делает это не только для namespace
# "mainsail", а для любого namespace/key -- общая утилита, не только под
# пресеты.
#
# ФОРМАТ KEY -- у Moonraker key поддерживает точечную адресацию ВНУТРИ
# namespace (не отдельные документы, а путь в JSON-дереве значения этого
# namespace). Пример: namespace=mainsail, key=presets.presets.<uuid> пишет
# в mainsail -> presets -> presets -> <uuid>, не создавая отдельной записи
# "presets.presets.<uuid>" буквально. Подтверждено чтением реального
# обработчика на принтере (~/moonraker/moonraker/components/database.py,
# _handle_item_request: GET читает key как есть, POST принимает namespace/
# key/value и зовёт insert_item(namespace, key, val) -- та же point-path
# адресация что и у GET).
#
# Использование:
#   scripts/moonraker-db.sh get <namespace> [key]
#   scripts/moonraker-db.sh set <namespace> <key> '<json-значение>'
#   scripts/moonraker-db.sh delete <namespace> <key>
#   scripts/moonraker-db.sh --help
#
# <json-значение> передаётся КАК ЕСТЬ в тело запроса -- этот скрипт не
# парсит и не проверяет JSON (на рабочей машине в PATH нет ни jq, ни python,
# см. scripts/export-mainsail-ui.sh про то же самое ограничение), так что
# синтаксическая ошибка в JSON обнаружится по ответу Moonraker, а не заранее.
# Простую валидную JSON-строку легко собрать вручную или heredoc'ом -- см.
# scripts/set-filament-presets.sh для примера использования.
#
# ЧЕГО ЭТОТ СКРИПТ НЕ ДЕЛАЕТ: не трогает printer-configs/, не перезапускает
# Klipper, не деплоит ничего -- запись в БД Moonraker не требует рестарта
# Klipper вообще (Klipper эту базу не читает, ей пользуется только Moonraker/
# Mainsail). Безопасно запускать в любой момент, независимо от состояния
# печати/нагрева -- в отличие от scripts/deploy.sh здесь нет и не нужно
# жёстких проверок print_stats/idle_timeout.
set -euo pipefail

MOONRAKER_URL="${MOONRAKER_URL:-http://192.168.11.160:7125}"

usage() {
    cat <<'EOF'
Использование:
  scripts/moonraker-db.sh get <namespace> [key]
  scripts/moonraker-db.sh set <namespace> <key> '<json-значение>'
  scripts/moonraker-db.sh delete <namespace> <key>
  scripts/moonraker-db.sh --help

  get     без key -- весь namespace целиком; с key -- конкретный путь
          (точечная адресация внутри namespace, см. заголовок файла).
  set     записывает <json-значение> по <namespace>/<key>. Значение -- сырой
          JSON, без экранирования этим скриптом; кавычь сам.
  delete  удаляет <namespace>/<key>.

Примеры:
  scripts/moonraker-db.sh get mainsail
  scripts/moonraker-db.sh get mainsail presets.presets
  scripts/moonraker-db.sh set mainsail presets.presets.myid \
      '{"name":"PLA","gcode":"","values":{"extruder":{"bool":true,"type":"heater","value":205}}}'
  scripts/moonraker-db.sh delete mainsail presets.presets.myid

Переменная окружения MOONRAKER_URL переопределяет адрес Moonraker
(по умолчанию http://192.168.11.160:7125).
EOF
}

die() {
    echo "$*" >&2
    exit 1
}

check_connection() {
    if ! curl -s -m 5 "$MOONRAKER_URL/printer/info" | grep -q '"state"'; then
        die "Moonraker не отвечает на $MOONRAKER_URL/printer/info -- прерываю. Проверь сеть/принтер руками."
    fi
}

cmd_get() {
    local namespace="$1" key="${2:-}"
    check_connection
    local url="$MOONRAKER_URL/server/database/item?namespace=$namespace"
    [ -n "$key" ] && url="$url&key=$key"
    local response
    response="$(curl -s -m 8 "$url")"
    if [[ "$response" == '{"error":'* ]]; then
        echo "$response" >&2
        exit 1
    fi
    printf '%s\n' "$response"
}

cmd_set() {
    local namespace="$1" key="$2" value="$3"
    [ -n "$namespace" ] && [ -n "$key" ] && [ -n "$value" ] || die "set требует namespace, key и json-значение -- см. --help"
    check_connection
    local body
    body="$(printf '{"namespace":%s,"key":%s,"value":%s}' \
        "$(json_string "$namespace")" "$(json_string "$key")" "$value")"
    local response
    response="$(curl -s -m 8 -X POST "$MOONRAKER_URL/server/database/item" \
        -H 'Content-Type: application/json' \
        -d "$body")"
    if [[ "$response" == '{"error":'* ]]; then
        echo "Ошибка записи:" >&2
        echo "$response" >&2
        exit 1
    fi
    printf '%s\n' "$response"
}

cmd_delete() {
    local namespace="$1" key="$2"
    [ -n "$namespace" ] && [ -n "$key" ] || die "delete требует namespace и key -- см. --help"
    check_connection
    local response
    response="$(curl -s -m 8 -X DELETE "$MOONRAKER_URL/server/database/item?namespace=$namespace&key=$key")"
    if [[ "$response" == '{"error":'* ]]; then
        echo "$response" >&2
        exit 1
    fi
    printf '%s\n' "$response"
}

# Простое экранирование строки в JSON-строку -- namespace/key в этом проекте
# всегда простые (буквы/цифры/точки), без кавычек и спецсимволов, так что
# полноценный JSON-энкодер не нужен: только оборачиваем в кавычки.
json_string() {
    printf '"%s"' "$1"
}

case "${1:-}" in
    -h|--help|"") usage; [ "${1:-}" = "" ] && exit 1 || exit 0 ;;
    get)    shift; cmd_get "$@" ;;
    set)    shift; cmd_set "$@" ;;
    delete) shift; cmd_delete "$@" ;;
    *)      echo "Неизвестная команда: $1" >&2; usage >&2; exit 2 ;;
esac
