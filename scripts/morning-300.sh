#!/usr/bin/env bash
# ЗАПУСКАТЬ ПОСЛЕ ТОГО, КАК ТЕРМИСТОР ОБЖАТ ФОЛЬГИРОВАННЫМ СКОТЧЕМ (H18).
#
# Собирает всё утро в одну команду: проверяет датчик, находит настоящее
# равновесие, и только если оно позволяет - запускает печать на 300 мм/с.
# Каждый шаг может остановить последовательность, чтобы не печатать вслепую.
#
# ПОЧЕМУ ИМЕННО ТАК. За 18-19 августа было три попытки печати и все три
# сорвались, причём каждый раз я делал вывод из показаний датчика, который сам
# под подозрением. Итог - несколько часов на версии, которые потом рассыпались:
# просадка питания, слабый картридж, влияние стола. Все три опровергнуты
# собственными данными. Поэтому здесь порядок обратный: сначала доказать, что
# датчику можно верить, и только потом что-либо им мерить.
#
# Запуск:  bash scripts/morning-300.sh
#          bash scripts/morning-300.sh --check-only   (только диагностика)
set -uo pipefail

HOST="${HOST:-192.168.11.160}"
API="http://$HOST:7125"
CHECK_ONLY=0
[ "${1:-}" = "--check-only" ] && CHECK_ONLY=1

say() { printf '\n\033[1m== %s ==\033[0m\n' "$*"; }
die() { printf '\n\033[31m✗ %s\033[0m\n' "$*"; exit 1; }
ok()  { printf '\033[32m✓\033[0m %s\n' "$*"; }

q() { curl -s --max-time 10 "$API/printer/objects/query?$1"; }
gc() { curl -s --max-time 20 -X POST "$API/printer/gcode/script" -H 'Content-Type: application/json' -d "{\"script\":\"$1\"}" >/dev/null; }

temp() { q extruder | grep -o '"temperature":[0-9.]*' | head -1 | cut -d: -f2; }
pwm()  { q extruder | grep -o '"power":[0-9.]*'       | head -1 | cut -d: -f2; }

# ---------------------------------------------------------------- 0. состояние
say "0. Состояние машины"
st="$(curl -s --max-time 10 "$API/printer/info" | grep -o '"state":"[a-z]*"' | head -1 | cut -d'"' -f4)"
[ "$st" = "ready" ] || die "Klipper в состоянии '$st', а не ready. Сначала FIRMWARE_RESTART."
ok "Klipper ready"

ps="$(q print_stats | grep -o '"state":"[a-z]*"' | head -1 | cut -d'"' -f4)"
case "$ps" in printing|paused) die "Идёт печать ($ps). Дождитесь или отмените." ;; esac
ok "печати нет"

# ---------------------------------------------------- 1. стол пуст (по камере)
say "1. Стол"
shot="$(mktemp -u).jpg"
if curl -s --max-time 15 -o "$shot" "http://$HOST/webcam/?action=snapshot" && [ -s "$shot" ]; then
    ok "кадр снят: $shot"
    echo "  🔴 ПОСМОТРИТЕ НА НЕГО. Хоуминг Z поднимает стол к соплу и"
    echo "     останавливается по концевику, а не по препятствию."
    printf "  Стол пуст? [y/N] "
    read -r a </dev/tty
    [ "$a" = "y" ] || die "Прервано: уберите деталь со стола."
else
    die "Камера не ответила - проверьте стол глазами и запустите снова."
fi

# ------------------------------------------------- 2. шум датчика (без нагрева)
say "2. Шум датчика (30 с, без нагрева)"
: > /tmp/noise.$$
for _ in $(seq 1 30); do temp >> /tmp/noise.$$; sleep 1; done
read -r mean sd <<EOF
$(awk '{s+=$1; a[NR]=$1} END{m=s/NR; for(i=1;i<=NR;i++)v+=(a[i]-m)^2; printf "%.2f %.4f", m, sqrt(v/NR)}' /tmp/noise.$$)
EOF
rm -f /tmp/noise.$$
echo "  среднее $mean °C, разброс $sd"
awk -v s="$sd" 'BEGIN{exit !(s < 0.5)}' \
    && ok "шум в норме (эталон - стол, 0.180)" \
    || die "Разброс $sd - датчик дёргает даже в покое. Это уже электрика, а не посадка."

# ------------------------------------- 3. равновесие: ступенчатый прогрев
say "3. Равновесие (ступенями, ~6 мин)"
echo "  🔴 НЕ ОТХОДИТЕ ОТ МАШИНЫ. Аппаратной защиты от перегрева нет."
peak=0
for T in 180 210 235; do
    gc "M104 S$T"
    printf "  цель %s: " "$T"
    for _ in $(seq 1 20); do
        sleep 6
        s="$(curl -s --max-time 10 "$API/printer/info" | grep -o '"state":"[a-z]*"' | head -1 | cut -d'"' -f4)"
        [ "$s" = "ready" ] || { gc "TURN_OFF_HEATERS"; die "Klipper упал в '$s' на ступени $T. Это H18/H20 - разбираться, не печатать."; }
    done
    t="$(temp)"; p="$(pwm)"
    printf "достигнуто %s °C при мощности %s\n" "$t" "$p"
    peak="$t"
done
gc "TURN_OFF_HEATERS"

awk -v t="$peak" 'BEGIN{exit !(t > 228)}' \
    && ok "235 берётся - расплава хватит на 14.5 мм³/с" \
    || die "Дошли только до $peak °C. Печать на 300 не поедет: нужно 230+. Дальше - обдув радиатора (H20)."

# --------------------------------------------------------------- 4. сама печать
if [ "$CHECK_ONLY" = 1 ]; then
    say "--check-only: диагностика пройдена, печать не запускаю"
    exit 0
fi

say "4. Печать GOAL_300mms_0.08mm.gcode (~1 ч 23 мин)"
echo "  3796 печатных ходов на 300 мм/с, слой 0.08, ускорение 4000."
printf "  Запускаю? [y/N] "
read -r a </dev/tty
[ "$a" = "y" ] || die "Отменено."

curl -s --max-time 30 -X POST "$API/printer/print/start" \
     -H 'Content-Type: application/json' \
     -d '{"filename":"GOAL_300mms_0.08mm.gcode"}' >/dev/null
ok "запущено"

echo
echo "  Параллельно запустите сторож термистора:"
echo "    bash <скретчпад>/thermwatch.sh thermwatch-goal.log"
echo
echo "  🔴 ПОСЛЕ ПЕЧАТИ - замерить накопленный сдвиг, это и есть проверка"
echo "     слова «стабильные». PRINT_END паркуется в известную точку, поэтому:"
echo "       MEASURE_HOME AXIS=Y   (ожидается 5.000, отклонение = потеря)"
echo "       MEASURE_HOME AXIS=X"
