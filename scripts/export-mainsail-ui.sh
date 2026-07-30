#!/usr/bin/env bash
# Снимок пользовательских настроек Mainsail (имя принтера, раскладка дашборда,
# тема) из БАЗЫ MOONRAKER (namespace "mainsail") в printer-configs/.
#
# ВАЖНО, прочитать перед использованием:
# - Эти настройки пользователь меняет через сам веб-интерфейс Mainsail
#   (Settings -> General / Dashboard), а не через файлы конфига. Хранятся они
#   в БД Moonraker (moonraker.db / SQLite на принтере), а не в printer.cfg,
#   moonraker.conf и т.п. -- printer-configs/ их поэтому не отслеживал.
# - Это ОДНОСТОРОННИЙ снимок для наглядности и бэкапа на момент запуска.
#   Скрипт НИЧЕГО не пишет обратно на принтер и не является частью deploy.sh
#   -- deploy.sh коммитит только файлы из своего списка FILES=(...), этот
#   JSON туда не входит и никогда не заливается на принтер автоматически.
# - Импорт/восстановление из этого файла НЕ реализован и не поддерживается:
#   БД Moonraker -- живое состояние, релевантное конкретной инсталляции
#   (id виджетов, версии схемы), слепое накатывание чужого/старого снимка
#   обратно может не сработать так, как ожидается. Если понадобится
#   восстановить руками -- смотреть на этот файл как на референс и вносить
#   через Mainsail UI, не через API записи в БД.
# - Из namespace "mainsail" в снимок попадают только стабильные,
#   человекочитаемые ключи: general (имя принтера), dashboard (раскладка
#   виджетов), uiSettings (тема/цвета), control (шаг/скорость экструдера
#   в панели). Сознательно ИСКЛЮЧЕНЫ шумные/часто меняющиеся ключи той же
#   namespace, не несущие полезной информации для версионирования:
#     - gcodehistory  -- сырая история команд, отправленных из консоли
#     - notifications -- id и таймстемпы закрытых уведомлений
#     - gcodeViewer    -- производный кэш кинематики (дублирует printer.cfg)
#     - initVersion    -- служебная метка версии первой инициализации Mainsail
#
# Запуск: bash scripts/export-mainsail-ui.sh
# Когда запускать: вручную, после того как пользователь поменял имя принтера
# и/или раскладку дашборда в Mainsail и хочет зафиксировать это в репозитории.
set -euo pipefail

MOONRAKER_URL="${MOONRAKER_URL:-http://192.168.11.160:7125}"
NAMESPACE="mainsail"
OUT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../printer-configs" && pwd)/moonraker-db-mainsail-ui.json"
KEYS=(general dashboard uiSettings control)

echo "== Проверяю связь с Moonraker ($MOONRAKER_URL) =="
if ! curl -s -m 5 "$MOONRAKER_URL/printer/info" | grep -q '"state"'; then
    echo "Moonraker не отвечает -- прерываю. Проверь сеть/принтер руками." >&2
    exit 1
fi

# Достаёт value для одного ключа namespace через ?key=, чтобы не тащить
# в снимок весь namespace целиком (там же лежит шумный gcodehistory и т.п.)
# и не парсить JSON локально -- на машине нет ни jq, ни python в PATH,
# поэтому value вырезается по фиксированному префиксу/суффиксу, которые
# Moonraker всегда возвращает в одном и том же виде для этого эндпоинта.
fetch_key() {
    local key="$1" raw prefix value
    raw="$(curl -s -m 8 "$MOONRAKER_URL/server/database/item?namespace=$NAMESPACE&key=$key")"
    if [[ "$raw" != '{"result":'* ]]; then
        echo "Ошибка: ключ '$key' -- Moonraker вернул не то, что ожидалось:" >&2
        echo "  $raw" >&2
        exit 1
    fi
    prefix="{\"result\":{\"namespace\":\"$NAMESPACE\",\"key\":\"$key\",\"value\":"
    if [[ "$raw" != "$prefix"* ]]; then
        echo "Ошибка: не смог разобрать ответ для ключа '$key' (формат Moonraker изменился?):" >&2
        echo "  $raw" >&2
        exit 1
    fi
    value="${raw#"$prefix"}"
    value="${value%??}"   # снимает завершающие "}}"  (закрытие result{} и внешней {})
    printf '%s' "$value"
}

echo "== Забираю ключи namespace '$NAMESPACE': ${KEYS[*]} =="
declare -A VALUES
for k in "${KEYS[@]}"; do
    echo "-> $k"
    VALUES[$k]="$(fetch_key "$k")"
done

cat > "$OUT" <<JSON
{
  "_snapshot_meta": {
    "что_это": "Точечный снимок Moonraker DB, namespace '$NAMESPACE' (настройки Mainsail UI), НЕ printer.cfg-конфиг и НЕ деплоится scripts/deploy.sh",
    "источник": "$MOONRAKER_URL/server/database/item?namespace=$NAMESPACE",
    "снято": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
    "исключено_из_namespace": ["gcodehistory", "notifications", "gcodeViewer", "initVersion"]
  },
  "general": ${VALUES[general]},
  "dashboard": ${VALUES[dashboard]},
  "uiSettings": ${VALUES[uiSettings]},
  "control": ${VALUES[control]}
}
JSON

echo
echo "OK: снимок записан в $OUT"
echo "Дальше: git diff printer-configs/moonraker-db-mainsail-ui.json && git add/commit руками."
