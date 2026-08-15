#!/usr/bin/env bash
# Заводит пресеты температур Mainsail (Settings -> Presets) под пластики,
# которые реально есть у пользователя -- через scripts/moonraker-db.sh,
# без похода в веб-интерфейс руками (запись, не только чтение).
#
# СХЕМА ХРАНЕНИЯ -- вычитана из реального исходника Mainsail (mainsail-crew/
# mainsail, src/store/gui/presets/{types,actions,mutations}.ts), не угадана:
#   namespace: mainsail
#   key:       presets.presets.<uuid>   (точечный путь ВНУТРИ namespace --
#              не буквальная строка-ключ, см. заголовок moonraker-db.sh)
#   value:     {"name": str, "gcode": str, "values": {<heater>: {"bool":
#              bool, "type": "heater", "value": number}, ...}}
# <heater> -- ИМЕНА КАК В KLIPPER, не произвольные: подтверждено живым
# запросом GET /printer/objects/query?heaters на этой машине --
# available_heaters = ["heater_bed", "extruder"]. Другие имена Mainsail
# просто не покажет в форме пресета (PresetsForm.vue берёт список отсюда же).
#
# Значения температур -- те, что обсуждались и были даны пользователю в
# сессии 2026-08-15 под конкретные пластики, которые у него есть (PLA,
# PETG, PETG-CF, PETG-GF, ABS-CF, PA6-CF, TPU). НЕ гарантия того, что печать
# ими сейчас реально пойдёт -- на момент написания этого скрипта на машине
# ещё не решена отдельная проблема "пластик греется, но не проходит через
# сопло" (см. docs/printer-status.md, раздел про экструдер/хотэнд
# 2026-08-15) -- пресеты можно завести уже сейчас (это просто настройка UI,
# не действие на живом принтере), пользоваться ими для реальной печати --
# только после диагностики хотэнда.
#
# Идемпотентность: НЕТ. Повторный запуск создаст новые uuid и задублирует
# пресеты с теми же именами (Mainsail хранит пресеты по id, не по имени).
# Если нужно поменять уже заведённые пресеты -- через сам Mainsail UI
# (Settings -> Presets -> редактировать/удалить), это штатный путь; этот
# скрипт -- разовый инструмент первого заполнения, не источник истины на
# каждый запуск.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DB="$SCRIPT_DIR/moonraker-db.sh"

# name;extruder_target;bed_target
PRESETS=(
    "PLA;205;55"
    "PETG;240;75"
    "PETG-CF;250;80"
    "PETG-GF;250;80"
    "ABS-CF;255;100"
    "PA6-CF;270;90"
    "TPU;220;45"
)

new_id() {
    head -c 16 /dev/urandom | od -An -tx1 | tr -d ' \n'
}

for entry in "${PRESETS[@]}"; do
    IFS=';' read -r name extruder bed <<<"$entry"
    id="$(new_id)"
    value="$(printf '{"name":"%s","gcode":"","values":{"extruder":{"bool":true,"type":"heater","value":%s},"heater_bed":{"bool":true,"type":"heater","value":%s}}}' \
        "$name" "$extruder" "$bed")"
    echo "-> $name (extruder=$extruder heater_bed=$bed, id=$id)"
    bash "$DB" set mainsail "presets.presets.$id" "$value"
done

echo
echo "OK: заведено ${#PRESETS[@]} пресетов. Проверить: bash scripts/moonraker-db.sh get mainsail presets"
echo "Или глазами -- Mainsail -> Settings -> Presets."
