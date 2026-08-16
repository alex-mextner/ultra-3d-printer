#!/usr/bin/env bash
# Меняет nozzle_diameter в printer-configs/printer.cfg на новое значение,
# пересчитывает max_extrude_cross_section с реальным запасом (не отключая
# проверку) и запускает lint + deploy — одна команда вместо ручной правки.
#
# ПОЧЕМУ ЭТО НЕ ЖИВАЯ КОМАНДА НА ПРИНТЕРЕ: Klipper не умеет менять
# nozzle_diameter на лету (значение читается только при загрузке конфига,
# см. NOZZLE_STATUS в printer.cfg), а CLAUDE.md запрещает править конфиг
# прямо на устройстве по SSH — этот скрипт делает ровно то же самое, что и
# ручная правка (repo -> lint -> deploy), просто в одну команду вместо
# нескольких. Это "легко менять" в рамках реальных ограничений Klipper, а не
# обход их.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CFG="$SCRIPT_DIR/../printer-configs/printer.cfg"

usage() {
    cat <<'EOF'
Использование: scripts/set-nozzle-size.sh <диаметр_мм> [-- deploy-флаги]

Меняет [extruder] nozzle_diameter, пересчитывает max_extrude_cross_section
(2x честного дефолта Klipper = 2 * 4.0 * diameter^2 — тот же принцип запаса,
что уже применён в printer.cfg 2026-08-16), затем lint + deploy.

Всё после "--" передаётся напрямую в scripts/deploy.sh (--yes, --dry-run).

Примеры:
  scripts/set-nozzle-size.sh 0.4                 # правка + обычный деплой с вопросом
  scripts/set-nozzle-size.sh 0.6 -- --dry-run    # только посмотреть diff, не заливать
  scripts/set-nozzle-size.sh 0.4 -- --yes        # без вопроса (автономный режим)
EOF
}

if [[ $# -lt 1 || "$1" == "-h" || "$1" == "--help" ]]; then
    usage
    exit 1
fi

DIAMETER="$1"
shift
DEPLOY_ARGS=("$@")
if [[ "${DEPLOY_ARGS[0]:-}" == "--" ]]; then
    DEPLOY_ARGS=("${DEPLOY_ARGS[@]:1}")
fi

# Формат и разумные границы (0.1..1.2мм — реальные принтерные сопла все в
# этом диапазоне; за пределами это почти наверняка опечатка, а не сопло).
if ! [[ "$DIAMETER" =~ ^[0-9]+\.?[0-9]*$ ]]; then
    echo "Ошибка: '$DIAMETER' не похоже на число (пример: 0.4)" >&2
    exit 1
fi
if ! awk -v d="$DIAMETER" 'BEGIN { exit !(d >= 0.1 && d <= 1.2) }'; then
    echo "Ошибка: $DIAMETER мм вне разумного диапазона сопел (0.1..1.2) — опечатка?" >&2
    exit 1
fi

DIAMETER_FMT=$(awk -v d="$DIAMETER" 'BEGIN { printf "%.3f", d }')
DEFAULT_CROSS=$(awk -v d="$DIAMETER" 'BEGIN { printf "%.3f", 4.0 * d * d }')
CROSS_SECTION=$(awk -v d="$DIAMETER" 'BEGIN { printf "%.1f", 2 * 4.0 * d * d }')

if ! grep -q '^nozzle_diameter:' "$CFG"; then
    echo "Ошибка: не нашёл строку 'nozzle_diameter:' в $CFG — формат файла изменился?" >&2
    exit 1
fi
if ! grep -q '^max_extrude_cross_section:' "$CFG"; then
    echo "Ошибка: не нашёл строку 'max_extrude_cross_section:' в $CFG" >&2
    exit 1
fi

echo "== nozzle_diameter -> ${DIAMETER_FMT}мм, max_extrude_cross_section -> ${CROSS_SECTION}мм^2 (Klipper default был бы ${DEFAULT_CROSS}) =="

# Меняем ТОЛЬКО первую строку каждого значения — многострочные пояснения
# ниже (почему это конфиг-тайм значение, откуда взялась формула запаса)
# оставляем как есть: они по-прежнему верны и относятся к МЕХАНИЗМУ, а не к
# конкретному числу. При повторных заменах старая история в комментарии
# может немного устареть по конкретным цифрам — deploy.sh покажет diff перед
# заливкой, это штатное место заметить и поправить руками при желании.
sed -i "s/^nozzle_diameter:.*/nozzle_diameter: ${DIAMETER_FMT}      # SET via scripts\/set-nozzle-size.sh (see git log for when\/why)/" "$CFG"
sed -i "s/^max_extrude_cross_section:.*/max_extrude_cross_section: ${CROSS_SECTION} # SET via scripts\/set-nozzle-size.sh - 2x Klipper's default (4.0*diameter^2 = ${DEFAULT_CROSS}mm^2), real margin without disabling the check./" "$CFG"

echo "== Lint =="
bash "$SCRIPT_DIR/lint-configs.sh"

echo "== Deploy =="
bash "$SCRIPT_DIR/deploy.sh" "${DEPLOY_ARGS[@]}"
