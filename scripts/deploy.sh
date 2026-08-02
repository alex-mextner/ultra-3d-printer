#!/usr/bin/env bash
# Синхронизация конфигов из printer-configs/ на принтер + рестарт Klipper.
#
# Конфиги правим ТОЛЬКО тут, в репозитории (см. CLAUDE.md) — не на самом принтере
# по SSH. Этот скрипт — единственный путь донести правки до Klipper.
#
# --- ЧТО ИЗМЕНИЛОСЬ 2026-08-01 (решение пользователя) -----------------------
# Раньше в шапке стояло «запускать самому, стоя рядом с принтером», а гейтом был
# вопрос «Стол/хотэнд должны быть под присмотром. [y/N]». Этот вопрос не проверял
# НИЧЕГО, кроме факта нажатия y, и мешал запускать деплой по команде пользователя
# без его личного присутствия у клавиатуры. Он заменён машинными проверками.
#
# Обоснование. Деплой = копирование файлов по scp + POST /printer/restart.
# Рестарт Klipper ГАСИТ все нагреватели и обрывает движение — то есть по 220В он
# безопаснее простоя (простой оставляет стол греться и дальше, рестарт — нет).
# Испортить он может только работу, которую нельзя прерывать. Значит безопасность
# теперь обеспечивает не «человек рядом», а вот эти три проверки состояния:
#
#   ЖЁСТКАЯ БЛОКИРОВКА (выход с ненулевым кодом, ничего не заливается):
#     1. print_stats.state == printing | paused — идёт (или приостановлена) печать.
#     2. idle_timeout.state == Printing — прямо сейчас выполняется gcode. Ловит и
#        ручной G28/перемещение из Mainsail: без этой проверки деплой оборвал бы
#        хоуминг посреди движения.
#   ПРЕДУПРЕЖДЕНИЕ (печатаем, но продолжаем):
#     3. ненулевой target у extruder / heater_bed — идёт прогрев, он собьётся.
#        Не опасно (рестарт всё равно всё погасит), просто обидно ждать заново.
#
# Жёсткие проверки выполняются ВСЕГДА, в обоих режимах: --yes пропускает только
# интерактивный вопрос, но НЕ проверки 1 и 2.
#
# Неизвестное состояние = ОТКАЗ, а не «наверное, можно». Если Moonraker не
# ответил или поле не разобралось — скрипт останавливается и говорит, что
# проверить руками. Молча деплоить поверх состояния, которое не удалось
# прочитать, опаснее, чем лишний раз остановиться.
# Исключение ровно одно и оно осознанное: если сам Klipper не в состоянии `ready`
# (error / shutdown / startup), объектные запросы к нему недоступны в принципе —
# но и печатать он в таком состоянии не может, прерывать нечего. Это как раз тот
# случай, когда деплой и нужен: залить исправленный конфиг и перезапуститься.
# Тогда проверки 1-3 пропускаются с явным сообщением в выводе.
#
# Использование:
#   scripts/deploy.sh              # проверки + diff + вопрос в терминале
#   scripts/deploy.sh --dry-run    # только проверки и diff, ничего не заливает
#   scripts/deploy.sh --yes        # автономно, без вопроса (проверки 1-2 остаются)
set -euo pipefail

HOST="${PRINTER_HOST:-ultra@192.168.11.160}"
API="${PRINTER_API:-http://192.168.11.160:7125}"
REMOTE_DIR="printer_data/config"
LOCAL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../printer-configs" && pwd)"
# smoke-alarm.cfg добавлен 2026-08-02: printer.cfg его [include]-ит, значит без
# него Klipper не поднимется. Файла на принтере пока нет — цикл диффа ниже это
# переживает (пустой remote_content => файл считается изменённым и заливается,
# бэкап делается через `cp ... || true`).
FILES=(printer.cfg smoke-alarm.cfg moonraker.conf crowsnest.conf autotune_tmc.cfg KlipperScreen.conf mainsail.cfg)

ASSUME_YES=0
DRY_RUN=0

usage() {
    cat <<'EOF'
Использование: scripts/deploy.sh [--yes|-y] [--dry-run] [--help]

  (без флагов)  показать diff и спросить подтверждение в терминале
  --yes, -y     автономный режим: не спрашивать. Жёсткие проверки
                (идёт печать / выполняется gcode) при этом НЕ отключаются.
  --dry-run     прогнать проверки и показать diff, ничего не заливать
  --help, -h    эта справка
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        -y|--yes)     ASSUME_YES=1 ;;
        --dry-run)    DRY_RUN=1 ;;
        -h|--help)    usage; exit 0 ;;
        *)            echo "Неизвестный аргумент: $1" >&2; usage >&2; exit 2 ;;
    esac
    shift
done

die() {
    echo "$*" >&2
    exit 1
}

# GET /printer/objects/query?<объект>. Никогда не валит скрипт сам по себе:
# пустой ответ разбирается дальше как «состояние неизвестно» → отказ.
api_query() {
    curl -s -m 8 "$API/printer/objects/query?$1" || true
}

# Достаёт первое значение ключа из плоского JSON. jq на рабочей машине нет
# (Git Bash под Windows), python в PATH тоже нет — тащить зависимость ради
# одного поля незачем, поэтому grep+sed. Каждый объект запрашивается отдельным
# запросом, чтобы одноимённые ключи разных объектов (у print_stats и
# idle_timeout оба называются "state") не смешивались в одном ответе.
# Если ключа нет — печатает пустую строку и возвращает 0 (проверку на пустоту
# делает вызывающая сторона, чтобы отличить «нет данных» от «значение 0»).
json_field() {
    printf '%s' "$1" | grep -o "\"$2\":[^,}]*" | head -n1 | sed 's/^[^:]*://; s/^"//; s/"$//' || true
}

echo "== Проверяю связь с принтером =="
INFO="$(curl -s -m 5 "$API/printer/info" || true)"
KLIPPY_STATE="$(json_field "$INFO" state)"

# Три разных исхода, их важно не смешивать:
#   пустое тело           -> молчит сам Moonraker (или сеть) => отказ;
#   тело есть, state есть -> нормальный ответ;
#   тело есть, state нет  -> Moonraker жив, но klippy к нему не подключён
#                            (служба klipper упала/остановлена — тогда Moonraker
#                            отдаёт 503 и {"error":{...}} без поля state).
#                            Печатать в этом состоянии нечем, прерывать нечего,
#                            а деплой как раз и нужен — обрабатываем как «не ready».
# Тело, не похожее на ответ Moonraker вообще (ни result, ни error — например
# HTML от чужого сервера на этом порту), считаем неизвестным состоянием => отказ.
if [ -z "$INFO" ]; then
    die "Принтер не отвечает на $API/printer/info — прерываю.
Проверь руками: curl $API/printer/info  и  ssh $HOST"
fi
if [ -z "$KLIPPY_STATE" ]; then
    case "$INFO" in
        *'"result"'*|*'"error"'*)
            KLIPPY_STATE="klippy-не-подключён"
            ;;
        *)
            die "На $API/printer/info пришёл ответ, не похожий на Moonraker — состояние неизвестно, отказываюсь деплоить.
Проверь руками: curl $API/printer/info"
            ;;
    esac
fi
echo "Klipper: $KLIPPY_STATE"

if [ "$KLIPPY_STATE" != "ready" ]; then
    echo
    echo "ВНИМАНИЕ: Klipper не в состоянии ready — печать сейчас идти не может,"
    echo "прерывать нечего, поэтому проверки печати/gcode/нагрева пропускаю."
    echo "Обычно это как раз тот случай, когда деплой и нужен: залить"
    echo "исправленный конфиг и перезапуститься."
    if [ "$KLIPPY_STATE" = "klippy-не-подключён" ]; then
        echo "(Moonraker отвечает, но klippy к нему не подключён — вероятно упала или"
        echo " остановлена служба klipper. Если после деплоя рестарт не поднимет её:"
        echo " ssh $HOST 'sudo systemctl restart klipper')"
    fi
else
    echo
    echo "== Проверяю, что принтер ничем не занят =="

    PS_JSON="$(api_query print_stats)"
    PRINT_STATE="$(json_field "$PS_JSON" state)"
    [ -n "$PRINT_STATE" ] || die "Не удалось прочитать print_stats.state — состояние печати неизвестно, отказываюсь деплоить.
Проверь руками: curl '$API/printer/objects/query?print_stats'"
    echo "  print_stats.state  = $PRINT_STATE"
    if [ "$PRINT_STATE" = "printing" ] || [ "$PRINT_STATE" = "paused" ]; then
        die "СТОП: идёт печать (print_stats.state=$PRINT_STATE).
Деплой перезапускает Klipper и оборвал бы её. Дождись конца или отмени печать."
    fi

    IT_JSON="$(api_query idle_timeout)"
    IDLE_STATE="$(json_field "$IT_JSON" state)"
    [ -n "$IDLE_STATE" ] || die "Не удалось прочитать idle_timeout.state — неизвестно, выполняется ли gcode, отказываюсь деплоить.
Проверь руками: curl '$API/printer/objects/query?idle_timeout'"
    echo "  idle_timeout.state = $IDLE_STATE"
    if [ "$IDLE_STATE" = "Printing" ]; then
        die "СТОП: прямо сейчас выполняется gcode (idle_timeout.state=Printing).
Это может быть ручной G28 или перемещение из Mainsail — рестарт Klipper оборвёт
движение посреди хода. Дождись окончания."
    fi

    # Нагрев — ТОЛЬКО предупреждение, никогда не блокировка: рестарт его всё равно
    # погасит, это не опасно, просто придётся греть заново. Поэтому и неразобранный
    # target здесь тоже не отказ, а предупреждение — в отличие от проверок 1-2 выше,
    # где неизвестное состояние блокирует. Разница осознанная: там от ответа зависит,
    # не оборвём ли мы работу, здесь — только удобство.
    HEATING=()
    for h in extruder heater_bed; do
        h_json="$(api_query "$h")"
        h_target="$(json_field "$h_json" target)"
        if [ -z "$h_target" ]; then
            HEATING+=("$h -> target не прочитался")
        elif awk -v t="$h_target" 'BEGIN { exit !(t + 0 > 0) }'; then
            HEATING+=("$h -> ${h_target}")
        fi
    done
    if [ ${#HEATING[@]} -gt 0 ]; then
        echo "  ПРЕДУПРЕЖДЕНИЕ: рестарт Klipper погасит нагреватели. Не опасно, но"
        echo "  греть придётся заново. Сейчас:"
        for x in "${HEATING[@]}"; do
            echo "    $x"
        done
    else
        echo "  нагреватели       = выключены (target 0)"
    fi
fi

echo
echo "== Что изменится (diff локальной версии с той, что реально стоит на принтере) =="
CHANGED=()
for f in "${FILES[@]}"; do
    if [ ! -f "$LOCAL_DIR/$f" ]; then
        continue
    fi
    remote_content="$(ssh -n -o ConnectTimeout=8 "$HOST" "cat $REMOTE_DIR/$f 2>/dev/null" || true)"
    if [ "$remote_content" != "$(cat "$LOCAL_DIR/$f")" ]; then
        echo "--- $f отличается ---"
        diff <(echo "$remote_content") "$LOCAL_DIR/$f" || true
        CHANGED+=("$f")
    fi
done

if [ ${#CHANGED[@]} -eq 0 ]; then
    echo "Изменений нет, деплоить нечего."
    exit 0
fi

echo
echo "Будут обновлены: ${CHANGED[*]}"

if [ "$DRY_RUN" -eq 1 ]; then
    echo "--dry-run: проверки пройдены, diff показан, ничего не заливаю."
    exit 0
fi

if [ "$ASSUME_YES" -eq 1 ]; then
    echo "--yes: подтверждение пропускаю (жёсткие проверки выше уже пройдены)."
else
    # Читаем именно из /dev/tty, а не из stdin: ssh в цикле диффа выше исторически
    # вычерпывал stdin, и подтверждение получало EOF (был баг, GitHub issue #4).
    # Плюс EOF/отсутствие терминала НИКОГДА не считается согласием — это отказ.
    # Проверяем терминал НАСТОЯЩИМ открытием в подоболочке, а не `[ -r /dev/tty ]`:
    # в Git Bash под Windows путь /dev/tty существует и проходит -r, но открыть его
    # без управляющего терминала нельзя ("No such device or address").
    if ! (exec 3</dev/tty) 2>/dev/null; then
        die "Терминала для подтверждения нет (неинтерактивный запуск).
Если это осознанный автономный деплой — перезапусти с --yes."
    fi
    echo "Дальше: бэкап этих файлов на принтере, заливка новых, рестарт Klipper."
    echo "Рестарт погасит все нагреватели и оборвёт любое текущее движение."
    answer=""
    read -r -p "Продолжить? [y/N] " answer < /dev/tty || answer=""
    if [ "$answer" != "y" ] && [ "$answer" != "Y" ]; then
        echo "Отменено."
        exit 0
    fi
fi

TS="$(ssh -n "$HOST" "date +%Y%m%d%H%M%S")"
for f in "${CHANGED[@]}"; do
    echo "-> бэкап $f -> $f.bak.$TS"
    ssh -n "$HOST" "cp $REMOTE_DIR/$f $REMOTE_DIR/$f.bak.$TS 2>/dev/null || true"
    echo "-> заливаю $f"
    scp "$LOCAL_DIR/$f" "$HOST:$REMOTE_DIR/$f"
done

echo
echo "== Перезапускаю Klipper и жду 'ready' =="
curl -s -X POST "$API/printer/restart" >/dev/null

ok=0
for i in $(seq 1 15); do
    sleep 2
    state="$(curl -s -m 5 "$API/printer/info" | grep -o '"state":"[a-z]*"' || true)"
    echo "  [$i/15] $state"
    if echo "$state" | grep -q ready; then
        ok=1
        break
    fi
done

if [ "$ok" -eq 1 ]; then
    echo "OK: printer/info вернул ready."
else
    echo "ВНИМАНИЕ: ready не дождались за 30с. Файлы УЖЕ залиты, Klipper не поднялся —
скорее всего ошибка в конфиге. Смотри klippy.log и Mainsail:
  ssh $HOST 'tail -40 printer_data/logs/klippy.log'
Откатиться можно бэкапами $REMOTE_DIR/*.bak.$TS, сделанными выше.
Нагреватели при этом выключены: они гаснут при рестарте и не включатся, пока
Klipper не запустится." >&2
    exit 1
fi
