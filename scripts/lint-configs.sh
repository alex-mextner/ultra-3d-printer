#!/usr/bin/env bash
# Klipper config linter — runs scripts/lint_configs.py against the REAL
# Klipper parsing pipeline (configfile.ConfigFileReader + jinja2, both
# imported from the printer's own klippy-env, not a local reimplementation).
#
# WHY it round-trips over SSH instead of running locally: this dev machine has
# no python3 in PATH at all, and even where python3+jinja2 exist locally, a
# "looks like Klipper's parser" reimplementation is exactly what gave a false
# negative on the real bug this linter exists to catch (see lint_configs.py's
# module docstring and printer-configs/power-loss-recovery.cfg's '\x23' fix).
# The printer's klippy-env (~/klippy-env/bin/python3) is the one interpreter
# proven to match what Klipper itself does with these files.
#
# What it checks (see lint_configs.py docstring for the full why):
#   1. JINJA     every gcode_macro/delayed_gcode gcode: value actually
#                Jinja-compiles under Klipper's own Environment.
#   2. INCLUDE   every [include X] in printer.cfg resolves to a real file —
#                this is what would have caught tonight's actual incident
#                (electronics-fan.cfg/rgb-status.cfg missing from deploy).
#   3. DUPLICATE no section name repeats across the files printer.cfg's
#                include tree actually pulls in (configparser strict=False
#                merges duplicates silently — no error, just a silent override).
#   4. PIN       no AVR pin is claimed by more than one section.
#
# Используется как:
#   - жёсткий pre-flight гейт в scripts/deploy.sh (перед проверками состояния
#     печати — правда, не потому что дешевле: обе стороны одинаково зависят
#     от доступности принтера по сети, просто раньше в порядке проверок);
#   - git pre-commit хук (scripts/git-hooks/pre-commit) — тот вызывает этот
#     скрипт с ЯВНЫМ путём к каталогу, собранному из проиндексированного
#     (staged), а не рабочего дерева — см. аргумент ниже и комментарий в
#     самом хуке про то, почему это обязательно, а не удобство.
#
# Аргумент (необязательный): каталог с *.cfg для проверки. По умолчанию —
# printer-configs/ рабочего дерева (обычный запуск руками/из deploy.sh).
# Явно передаётся хуком, когда нужно проверить не рабочее дерево, а то, что
# реально попадёт в коммит.
#
# Exit codes: 0 = чисто. 1 = реальная проблема (см. вывод lint_configs.py).
# 2 = сам линтер не смог отработать (принтер недоступен, klippy-env пропал и
# т.п.). Тот же принцип, что и в проверках состояния печати deploy.sh:
# неизвестный результат — это отказ, а не "наверное, всё чисто".
set -euo pipefail

HOST="${PRINTER_HOST:-ultra@192.168.11.160}"
LOCAL_DIR="${1:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../printer-configs" && pwd)}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REMOTE_TMP="/tmp/lint-configs-$$"
REMOTE_TMP_CREATED=0

die() {
    echo "$*" >&2
    exit 2
}

cleanup() {
    if [ "$REMOTE_TMP_CREATED" -eq 1 ]; then
        ssh -n -o ConnectTimeout=8 "$HOST" "rm -rf '$REMOTE_TMP'" >/dev/null 2>&1 || true
    fi
}
trap cleanup EXIT

if [ ! -d "$LOCAL_DIR" ]; then
    die "Каталог для проверки не существует: $LOCAL_DIR"
fi

echo "== Линтер конфигов: копирую *.cfg + lint_configs.py на принтер =="
if ! ssh -n -o ConnectTimeout=8 "$HOST" "mkdir -p '$REMOTE_TMP'"; then
    die "Не смог создать $REMOTE_TMP на $HOST — принтер недоступен? Отказываюсь (неизвестное состояние = отказ).
Проверь руками: ssh $HOST true"
fi
REMOTE_TMP_CREATED=1

# Только *.cfg верхнего уровня — то же множество файлов, что видит check 1/4.
# .conf (moonraker/crowsnest/KlipperScreen) не парсятся Klipper'ом вообще и в
# лоте не участвуют. KlipperScreen-themes/ — директория, под *.cfg не подпадает,
# так и задумано (это не конфиг Klipper, см. комментарий в deploy.sh почему
# у неё вообще другой механизм деплоя).
shopt -s nullglob
CFG_FILES=("$LOCAL_DIR"/*.cfg)
shopt -u nullglob
if [ ${#CFG_FILES[@]} -eq 0 ]; then
    die "В $LOCAL_DIR не нашлось ни одного *.cfg — это не похоже на правду, отказываюсь."
fi

if ! scp -q "${CFG_FILES[@]}" "$SCRIPT_DIR/lint_configs.py" "$HOST:$REMOTE_TMP/"; then
    die "Не смог скопировать файлы на принтер — отказываюсь."
fi

echo "== Прогоняю через klippy-env (реальные configfile.py + jinja2) =="
set +e
OUTPUT="$(ssh -n -o ConnectTimeout=8 "$HOST" "~/klippy-env/bin/python3 '$REMOTE_TMP/lint_configs.py' '$REMOTE_TMP'" 2>&1)"
RC=$?
set -e

echo "$OUTPUT"

if [ $RC -eq 0 ]; then
    exit 0
elif [ $RC -eq 1 ]; then
    echo
    echo "СТОП: линтер нашёл реальную проблему в printer-configs/ (см. выше)." >&2
    exit 1
else
    echo
    echo "СТОП: линтер не смог отработать (код $RC) — принтер/klippy-env недоступны" >&2
    echo "или сломались сами. Неизвестное состояние = отказ, не 'наверное чисто'." >&2
    exit 2
fi
