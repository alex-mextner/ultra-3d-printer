#!/usr/bin/env bash
# Deploy klipper-extras/*.py to ~/klipper/klippy/extras/ on the printer,
# plus the printer-configs/*.cfg files that reference them, and restart
# Klipper. Separate from scripts/deploy.sh on purpose: that script's glob
# only covers printer-configs/*.cfg and *.conf (see its own header on the
# 2026-08-07 incident that made it a glob in the first place) - a .py file
# dropped into klippy/extras/ needs a different remote path
# (~/klipper/klippy/extras/, not printer_data/config) entirely outside that
# glob's reach, so it needs its own script rather than bending deploy.sh's
# now-hardened glob logic to cover two unrelated directories.
#
# Reuses the same "unknown state = refuse" print-state safety gates as
# deploy.sh (see that script's header for the full reasoning): restarting
# Klipper mid-print or mid-gcode is exactly as unsafe here as it is there,
# for the same reason (POST /printer/restart kills heaters and aborts
# motion) - the gates are copied, not imported, so this script has no
# runtime dependency on deploy.sh's internals changing out from under it.
#
# Usage:
#   scripts/deploy-klipper-extras.sh              # diff + confirm in terminal
#   scripts/deploy-klipper-extras.sh --dry-run     # checks + diff only
#   scripts/deploy-klipper-extras.sh --yes         # unattended (hard gates stay on)
set -euo pipefail

HOST="${PRINTER_HOST:-ultra@192.168.11.160}"
API="${PRINTER_API:-http://192.168.11.160:7125}"
REMOTE_EXTRAS_DIR="klipper/klippy/extras"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
LOCAL_EXTRAS_DIR="$REPO_DIR/klipper-extras"

ASSUME_YES=0
DRY_RUN=0

usage() {
    cat <<'EOF'
Использование: scripts/deploy-klipper-extras.sh [--yes|-y] [--dry-run] [--help]

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

shopt -s nullglob
PY_FILES_FOUND=("$LOCAL_EXTRAS_DIR"/*.py)
shopt -u nullglob
if [ ${#PY_FILES_FOUND[@]} -eq 0 ]; then
    die "В $LOCAL_EXTRAS_DIR не нашлось ни одного *.py — нечего деплоить."
fi
PY_FILES=()
for f in "${PY_FILES_FOUND[@]}"; do
    PY_FILES+=("$(basename "$f")")
done

api_query() {
    curl -s -m 8 "$API/printer/objects/query?$1" || true
}

json_field() {
    printf '%s' "$1" | grep -o "\"$2\":[^,}]*" | head -n1 | sed 's/^[^:]*://; s/^"//; s/"$//' || true
}

echo "== Проверяю связь с принтером =="
INFO="$(curl -s -m 5 "$API/printer/info" || true)"
KLIPPY_STATE="$(json_field "$INFO" state)"

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
echo "== Что изменится =="
CHANGED=()
for f in "${PY_FILES[@]}"; do
    remote_content="$(ssh -n -o ConnectTimeout=8 "$HOST" "cat $REMOTE_EXTRAS_DIR/$f 2>/dev/null" || true)"
    if [ "$remote_content" != "$(cat "$LOCAL_EXTRAS_DIR/$f")" ]; then
        echo "--- $f отличается (или отсутствует на принтере) ---"
        CHANGED+=("$f")
    fi
done

if [ ${#CHANGED[@]} -eq 0 ]; then
    echo "Изменений нет, деплоить нечего."
    exit 0
fi

echo
echo "Будут обновлены: ${CHANGED[*]}"
echo "(конфиг printer-configs/axis-travel-measure.cfg и [include] в printer.cfg"
echo " заливаются отдельно, обычным scripts/deploy.sh — это скрипт только для .py)"

if [ "$DRY_RUN" -eq 1 ]; then
    echo "--dry-run: проверки пройдены, diff показан, ничего не заливаю."
    exit 0
fi

if [ "$ASSUME_YES" -eq 1 ]; then
    echo "--yes: подтверждение пропускаю (жёсткие проверки выше уже пройдены)."
else
    if ! (exec 3</dev/tty) 2>/dev/null; then
        die "Терминала для подтверждения нет (неинтерактивный запуск).
Если это осознанный автономный деплой — перезапусти с --yes."
    fi
    echo "Дальше: заливка .py в $REMOTE_EXTRAS_DIR, рестарт Klipper."
    echo "Рестарт погасит все нагреватели и оборвёт любое текущее движение."
    answer=""
    read -r -p "Продолжить? [y/N] " answer < /dev/tty || answer=""
    if [ "$answer" != "y" ] && [ "$answer" != "Y" ]; then
        echo "Отменено."
        exit 0
    fi
fi

for f in "${CHANGED[@]}"; do
    echo "-> заливаю $f"
    scp "$LOCAL_EXTRAS_DIR/$f" "$HOST:$REMOTE_EXTRAS_DIR/$f"
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
    echo "Проверка объекта: curl '$API/printer/objects/query?axis_travel_report'"
else
    echo "ВНИМАНИЕ: ready не дождались за 30с. Файл(ы) УЖЕ залиты, Klipper не поднялся —
скорее всего ошибка импорта модуля. Смотри klippy.log:
  ssh $HOST 'tail -60 printer_data/logs/klippy.log'" >&2
    exit 1
fi
