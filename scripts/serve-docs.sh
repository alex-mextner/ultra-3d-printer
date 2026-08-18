#!/usr/bin/env bash
# Публикация портала docs/ на принтер: http://192.168.11.160:8001/
#
# Заменил docs/serve-ramps.sh (порт 8000, локальный python, один файл). Причины
# перехода — обе практические:
#   1. Локальный сервер на рабочей машине не переживал перезапуск песочницы
#      между заходами (см. историю в printer-status.md), а принтер работает
#      всегда — он и так включён, когда документация нужна.
#   2. Читают документацию С ПЛАНШЕТА У СТАНКА, а не с той машины, где её пишут.
#      localhost:8000 планшету недоступен, 192.168.11.160:8001 — доступен.
#
# Что делает: заливает страницы портала и сам сервер в /tmp/docsrv на принтере,
# перезапускает сервер, проверяет, что корень отвечает 200 и что это индекс.
# Идемпотентен: повторный запуск просто перезальёт файлы и перезапустит сервер.
#
# Использование:
#   bash scripts/serve-docs.sh              # залить и перезапустить
#   bash scripts/serve-docs.sh --check      # ничего не заливать, только проверить
#   bash scripts/serve-docs.sh --stop       # погасить сервер на принтере
#
# ВАЖНО про удалённые команды: НЕ гасить сервер через pkill с шаблоном, который
# совпадает с командной строкой самой ssh-сессии — ssh при этом убивает сам себя
# и возвращает 255. Правильный способ ровно один: fuser -k -n tcp <порт>.
set -euo pipefail

HOST="${DOCS_HOST:-root@192.168.11.160}"
HOST_ADDR="${HOST##*@}"
PORT="${DOCS_PORT:-8001}"
REMOTE_DIR="${DOCS_REMOTE_DIR:-/tmp/docsrv}"
URL="http://${HOST_ADDR}:${PORT}/"

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DOCS_DIR="$REPO/docs"

# Явный список — НЕ `docs/*`. В docs/ лежит образ прошивки OpenWrt на 7.6 МБ, а
# /tmp на принтере это tmpfs, то есть та же оперативная память, которой на
# Orange Pi One 512 МБ. Заливать туда лишнее нельзя.
PAGES=(
    index.html
    safety.html
    ramps-diagnostics.html
    reference.html
    slicer-profile-install.html
    portal.css
)
# safety.html — единственная страница портала ПРО ЭТУ МАШИНУ, а не методика
# (сознательное исключение из правила «портал = общая методика», см. CLAUDE.md).
# Она здесь именно потому, что её читают у станка с планшета в момент, когда
# что-то пошло не так, а не из git. Текстовый оригинал — docs/safety.md, обе
# версии правятся вместе.
# «tmc2209 pinout.webp» отсюда убран 2026-08-03 вместе с картинкой распиновки
# модуля на reference.html: она показывала один вариант разводки из нескольких и
# врала про SPREAD. Заменена инлайновым SVG прямо в reference.html — заливать
# нечего. Сам файл в репозитории оставлен как исходник претензии.
ASSETS=(
    "ramps-v1.4.png"
    "Arduino-Mega-2560-Pinout.png"
    "arduino-mega-2560-pinout.pdf"
    "tmc2209-uart-mode-select.png"
    "orange-pi-one-pinout.png"
)

MODE=publish
case "${1:-}" in
    --check) MODE=check ;;
    --stop)  MODE=stop ;;
    --help|-h)
        sed -n '2,22p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
        exit 0
        ;;
    "") ;;
    *) echo "Неизвестный аргумент: $1 (см. --help)" >&2; exit 2 ;;
esac

say()  { printf '\n\033[1m%s\033[0m\n' "$*"; }
ok()   { printf '  \033[32m✓\033[0m %s\n' "$*"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$*"; }
die()  { printf '\n\033[31m✗ %s\033[0m\n' "$*" >&2; exit 1; }

# -n обязателен: без него ssh держит канал открытым, ожидая EOF на своём stdin,
# и вызов «запусти демон в фоне» не возвращается никогда, хотя демон уже работает.
ssh_do() { ssh -n -o ConnectTimeout=10 -o BatchMode=yes "$HOST" "$@"; }

# --- Останов сервера. Отдельной функцией, потому что и --stop, и перезапуск
#     обязаны гасить его ровно одним и тем же безопасным способом.
stop_server() {
    # fuser возвращает 1, когда порт никем не занят. Это НЕ ошибка (первый запуск,
    # или сервер уже погашен), поэтому `|| true` — иначе set -e убьёт скрипт.
    ssh_do "fuser -k -n tcp ${PORT} >/dev/null 2>&1 || true"
}

if [ "$MODE" = stop ]; then
    say "Останов сервера портала на ${HOST_ADDR}:${PORT}"
    stop_server
    ok "порт ${PORT} освобождён (или и не был занят)"
    exit 0
fi

# --- Проверка доступности: 200 + no-store + это действительно индекс -----------
check_live() {
    local body headers
    headers="$(curl -sS -m 10 -D - -o /dev/null "$URL" 2>/dev/null)" || return 1
    grep -qi '^HTTP/[0-9.]* 200' <<<"$headers" || { warn "корень ответил не 200:"; head -1 <<<"$headers"; return 1; }
    grep -qi '^Cache-Control:.*no-store' <<<"$headers" || { warn "нет заголовка Cache-Control: no-store"; return 1; }
    body="$(curl -sS -m 10 "$URL")" || return 1
    grep -q 'Портал документации' <<<"$body" || { warn "корень отвечает, но это не индекс портала"; return 1; }
    grep -qi 'http-equiv="refresh"\|meta refresh' <<<"$body" && { warn "на корне всё ещё редирект-заглушка"; return 1; }
    return 0
}

if [ "$MODE" = check ]; then
    say "Проверка портала: $URL"
    if check_live; then
        ok "200, Cache-Control: no-store, на корне индекс портала"
        for p in "${PAGES[@]}"; do
            code="$(curl -sS -m 10 -o /dev/null -w '%{http_code}' "${URL}${p}")"
            [ "$code" = 200 ] && ok "$p → $code" || warn "$p → $code"
        done
        exit 0
    fi
    die "портал не отвечает как надо — запустите без --check, чтобы перезалить"
fi

# --- Публикация ---------------------------------------------------------------
say "Публикация портала docs/ → ${HOST}:${REMOTE_DIR}"

for p in "${PAGES[@]}"; do
    [ -f "$DOCS_DIR/$p" ] || die "нет файла docs/$p — портал неполный, заливать нечего"
done

ssh_do "mkdir -p '${REMOTE_DIR}'" || die "не достучаться до ${HOST} по ssh"
ok "каталог ${REMOTE_DIR} готов"

# Страницы портала — обязательные.
for p in "${PAGES[@]}"; do
    scp -q "$DOCS_DIR/$p" "${HOST}:${REMOTE_DIR}/" || die "не удалось залить docs/$p"
    ok "залит $p"
done

# Сервер. Кладём под тем же именем, что и в репозитории, чтобы при разборе
# полётов на принтере было видно, откуда файл взялся.
scp -q "$REPO/scripts/docs-server.py" "${HOST}:${REMOTE_DIR}/docs-server.py" || die "не удалось залить docs-server.py"
ok "залит docs-server.py"

# Картинки — не блокирующие: портал без них читается, просто без иллюстраций.
for a in "${ASSETS[@]}"; do
    if [ -f "$DOCS_DIR/$a" ]; then
        if scp -q "$DOCS_DIR/$a" "${HOST}:${REMOTE_DIR}/"; then
            ok "залит $a"
        else
            warn "не удалось залить $a — страница схем будет с битой картинкой"
        fi
    else
        warn "нет файла docs/$a — пропущен"
    fi
done

# Старая временная заглушка с редиректом на ramps-diagnostics.html, если она
# осталась от прошлых сессий, теперь перезаписана настоящим index.html выше.
# Отдельно чистим только заведомо чужие остатки.
ssh_do "rm -f '${REMOTE_DIR}/serve.py'" || true

say "Перезапуск сервера"
stop_server
ok "старый процесс на порту ${PORT} снят"

# Запуск демона через ssh — место, где скрипт легче всего повесить намертво.
# Три вещи, каждая обязательна (проверено экспериментом 2026-08-03):
#   1. `ssh -n` — иначе ssh ждёт EOF на собственном stdin.
#   2. Перенаправления вешаются на ВЕСЬ фоновый блок `{ ...; }`, а не на одну
#      команду внутри. Форма `cd X && cmd >log 2>&1 &` НЕ РАБОТАЕТ: фоновая
#      подоболочка, выполняющая `cd`, продолжает держать stdout/stderr ssh-канала,
#      и ssh честно ждёт её закрытия — 12 секунд до таймаута вместо 2.
#   3. Клиентский timeout сверху: даже если что-то опять зависнет, вердикт
#      выносит HTTP-проверка ниже, а не эта команда.
timeout 25 ssh -n -o ConnectTimeout=10 -o BatchMode=yes "$HOST" \
    "{ cd '${REMOTE_DIR}' && DOCS_PORT=${PORT} setsid python3 docs-server.py; } >'${REMOTE_DIR}/srv.log' 2>&1 </dev/null & exit 0" \
    >/dev/null 2>&1 || warn "команда запуска не вернулась чисто — проверяю по HTTP"
ok "сервер запущен"

say "Проверка"
for attempt in 1 2 3 4 5 6 7 8 9 10; do
    if check_live; then
        ok "200, Cache-Control: no-store, на корне индекс портала"
        for p in "${PAGES[@]}"; do
            code="$(curl -sS -m 10 -o /dev/null -w '%{http_code}' "${URL}${p}")"
            [ "$code" = 200 ] && ok "$p → $code" || warn "$p → $code"
        done
        printf '\n\033[1m\033[32mГотово: %s\033[0m\n' "$URL"
        printf 'Открывать без ?v= — сервер отдаёт no-store, F5 всегда даёт свежую версию.\n\n'
        exit 0
    fi
    sleep 1
done

echo
ssh_do "tail -20 '${REMOTE_DIR}/srv.log' 2>/dev/null" || true
die "сервер поднялся, но $URL не отвечает как надо — лог выше"
