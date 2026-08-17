#!/usr/bin/env bash
# Deploy klipper-extras/*.py (this repo's own Klipper extension modules) onto
# the printer and restart Klipper.
#
# WHY A SEPARATE SCRIPT FROM deploy.sh. deploy.sh globs printer-configs/*.cfg
# and *.conf into printer_data/config (see its header for the 2026-08-07
# incident that turned its file list into a glob). A .py module belongs in
# klippy/extras/ instead - a completely different remote path, outside that
# glob's reach - so it gets its own script rather than a bent version of
# deploy.sh's now-hardened logic. The print-state safety gates below are COPIED
# from deploy.sh, not imported: restarting Klipper mid-print or mid-gcode is
# exactly as unsafe here, for the same reason (POST /printer/restart kills
# heaters and aborts motion), and this script must not break when deploy.sh's
# internals move.
#
# HISTORY, so this is not mistaken for a duplicate of something retired. An
# earlier version of this script existed and was deleted in 3f05eb6 when
# axis_travel_report.py moved to its own public git repo and a Moonraker
# [update_manager] entry. That retirement was right FOR THAT MODULE and is not
# reversed here: axis_travel_report.py is not in klipper-extras/ any more, so
# this script's glob cannot see it, and the symlink guard below refuses to
# touch a klippy/extras entry that points anywhere other than this script's own
# staging directory - so an update_manager-managed module can never be
# clobbered by a run of this script. What was left behind by that retirement is
# a real gap: klipper-extras/pid_calibrate_progress.py (fbc3574) and
# z_offset_guard.py have no publish-to-github pipeline and no way onto the
# machine at all. This closes it.
#
# LAYOUT ON THE PRINTER. Files are staged in ~/klipper-extras-local/ and
# SYMLINKED into ~/klipper/klippy/extras/, mirroring how axis_travel_report.py
# is installed. Two reasons over a plain copy: the klipper git clone stays free
# of foreign regular files, and `ls -la klippy/extras/` then shows at a glance
# which modules are local additions rather than stock Klipper.
#
# ONLY TOP-LEVEL klipper-extras/*.py IS DEPLOYED. Same lesson deploy.sh learned
# the hard way about its own glob, applied in the other direction: anything that
# must NOT reach the printer goes in a SUBDIRECTORY. klipper-extras/tests/ holds
# the modules' test harnesses (run them with the printer's own interpreter:
#   scp klipper-extras/z_offset_guard.py klipper-extras/tests/test_z_offset_guard.py \
#       ultra@192.168.11.160:/tmp/zguard-test/
#   ssh ultra@192.168.11.160 'cd /tmp/zguard-test && ~/klippy-env/bin/python \
#       test_z_offset_guard.py'
# ) and the glob below cannot see them.
#
# A MODULE FILE ON ITS OWN IS INERT. Klipper only imports klippy/extras/<name>.py
# when a [<name>] section exists in the config, so copying a .py here changes
# no behaviour by itself - the matching printer-configs/*.cfg (and its [include]
# in printer.cfg) still has to go through the normal scripts/deploy.sh. That is
# also the correct ORDER for a first install: this script first, deploy.sh
# second.
#
# Usage:
#   scripts/deploy-klipper-extras.sh              # diff + confirm in terminal
#   scripts/deploy-klipper-extras.sh --dry-run    # checks + diff only
#   scripts/deploy-klipper-extras.sh --yes        # unattended (hard gates stay on)
set -euo pipefail

HOST="${PRINTER_HOST:-ultra@192.168.11.160}"
API="${PRINTER_API:-http://192.168.11.160:7125}"
REMOTE_EXTRAS_DIR="klipper/klippy/extras"
REMOTE_STAGE_DIR="klipper-extras-local"
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

Заливает klipper-extras/*.py в ~/klipper-extras-local/ на принтере и
симлинкует в ~/klipper/klippy/extras/. Конфиги (.cfg) идут ОТДЕЛЬНО, обычным
scripts/deploy.sh — сам по себе модуль без своей [секции] в конфиге ничего не
делает и не грузится.
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

echo "== Локальная проверка синтаксиса модулей =="
# Компилируем каждый .py ЛОКАЛЬНО перед тем, как что-то заливать. Синтаксическая
# ошибка в модуле, у которого уже есть [секция] в конфиге, — это Klipper,
# который не поднимется после рестарта; поймать это до заливки дёшево.
# (Настоящую проверку — импорт в klippy-env на принтере — делает
# scripts/lint-configs.sh для конфигов и ручной прогон для модулей; тут именно
# синтаксис, без импорта зависимостей Klipper.)
PYBIN=""
for cand in python3 python py; do
    if command -v "$cand" >/dev/null 2>&1; then PYBIN="$cand"; break; fi
done
if [ -z "$PYBIN" ]; then
    echo "  python не найден локально — пропускаю проверку синтаксиса."
else
    for f in "${PY_FILES_FOUND[@]}"; do
        "$PYBIN" -m py_compile "$f" || die "Синтаксическая ошибка в $f — не заливаю ничего."
        echo "  OK: $(basename "$f")"
    done
fi

echo
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

    # Ещё одна жёсткая проверка, которой нет в deploy.sh и которая нужна именно
    # здесь: несохранённый SAVE_CONFIG. Рестарт Klipper выбрасывает всё, что
    # накопил configfile.set() и ещё не ушло в файл, — то есть свежие результаты
    # PID_CALIBRATE / Z_ENDSTOP_CALIBRATE пропадут молча.
    CF_JSON="$(api_query configfile)"
    SAVE_PENDING="$(json_field "$CF_JSON" save_config_pending)"
    if [ "$SAVE_PENDING" = "true" ]; then
        die "СТОП: у Klipper есть НЕСОХРАНЁННЫЕ изменения конфига
(configfile.save_config_pending = true). Рестарт их потеряет. Либо выполни
SAVE_CONFIG, либо осознанно сбрось их через RESTART, и запусти этот скрипт
заново."
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
echo "== Проверяю, что ни один симлинк в extras не будет затёрт чужим =="
# Отказ, а не «перезапишем»: axis_travel_report.py на этой машине —
# симлинк в отдельный git-клон, которым управляет Moonraker update_manager.
# Молча заменить такой симлинк своим значило бы отвязать модуль от его
# источника обновлений.
for f in "${PY_FILES[@]}"; do
    link_target="$(ssh -n -o ConnectTimeout=8 "$HOST" \
        "readlink $REMOTE_EXTRAS_DIR/$f 2>/dev/null" || true)"
    if [ -n "$link_target" ]; then
        case "$link_target" in
            */"$REMOTE_STAGE_DIR"/*) : ;;   # наш собственный, всё в порядке
            *)
                die "СТОП: $REMOTE_EXTRAS_DIR/$f — симлинк на $link_target,
а не на ~/$REMOTE_STAGE_DIR/. Скорее всего этим модулем управляет Moonraker
update_manager (как axis_travel_report.py). Замена симлинка отвязала бы его от
источника обновлений — разбирайся руками, автоматически не трогаю."
                ;;
        esac
    fi
done

echo
echo "== Что изменится =="
CHANGED=()
for f in "${PY_FILES[@]}"; do
    remote_content="$(ssh -n -o ConnectTimeout=8 "$HOST" \
        "cat $REMOTE_EXTRAS_DIR/$f 2>/dev/null" || true)"
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
echo "(соответствующие printer-configs/*.cfg и [include] в printer.cfg"
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
    echo "Дальше: заливка .py в ~/$REMOTE_STAGE_DIR + симлинки в $REMOTE_EXTRAS_DIR,"
    echo "затем рестарт Klipper."
    echo "Рестарт погасит все нагреватели и оборвёт любое текущее движение."
    answer=""
    read -r -p "Продолжить? [y/N] " answer < /dev/tty || answer=""
    if [ "$answer" != "y" ] && [ "$answer" != "Y" ]; then
        echo "Отменено."
        exit 0
    fi
fi

ssh -n -o ConnectTimeout=8 "$HOST" "mkdir -p ~/$REMOTE_STAGE_DIR"
for f in "${CHANGED[@]}"; do
    echo "-> заливаю $f"
    scp -q "$LOCAL_EXTRAS_DIR/$f" "$HOST:$REMOTE_STAGE_DIR/$f"
    ssh -n -o ConnectTimeout=8 "$HOST" \
        "ln -sfn \$HOME/$REMOTE_STAGE_DIR/$f \$HOME/$REMOTE_EXTRAS_DIR/$f"
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
    echo "Модули на месте:"
    ssh -n -o ConnectTimeout=8 "$HOST" "ls -la $REMOTE_EXTRAS_DIR | grep '^l'" || true
else
    echo "ВНИМАНИЕ: ready не дождались за 30с. Файл(ы) УЖЕ залиты, Klipper не поднялся —
скорее всего ошибка импорта модуля. Смотри klippy.log:
  ssh $HOST 'tail -60 printer_data/logs/klippy.log'" >&2
    exit 1
fi
