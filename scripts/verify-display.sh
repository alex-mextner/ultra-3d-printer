#!/usr/bin/env bash
# Быстрая проверка состояния экрана принтера после перезагрузки/сессии правок.
#
# Запускать С РАБОЧЕЙ МАШИНЫ (не на принтере). Скриншоты снимаются на принтере
# (дёшево), а всё увеличение/анализ — локально: тяжёлый ImageMagick на самом
# Orange Pi однажды уже исчерпал память и подвесил машину до ручного
# power-cycle, см. раздел про слабое железо в CLAUDE.md.
#
# Что делает:
#   1. Ждёт, пока принтер реально начнёт отвечать по SSH (не просто откроет порт).
#   2. Проверяет сервисы, память, был ли OOM в прошлой загрузке.
#   3. Проверяет, что все локальные правки KlipperScreen на месте.
#   4. Проверяет день/ночь-переключатель темы и его расписание.
#   5. Снимает скриншот главного экрана в текущую папку.
#
# Использование:  bash scripts/verify-display.sh
set -uo pipefail

HOST="${PRINTER_HOST:-root@192.168.11.160}"
OUT="${1:-display-check-$(date +%Y%m%d-%H%M%S).png}"

say() { printf '\n=== %s ===\n' "$1"; }

say "1. Жду доступности SSH (до 5 минут)"
deadline=$(( $(date +%s) + 300 ))
until timeout 12 ssh -o ConnectTimeout=8 -o BatchMode=yes "$HOST" true 2>/dev/null; do
    if [ "$(date +%s)" -ge "$deadline" ]; then
        echo "НЕ ДОЖДАЛСЯ. Если порт 22 открыт, но соединение рвётся до баннера" >&2
        echo "(kex_exchange_identification) — это исчерпание памяти, нужен" >&2
        echo "физический power-cycle, по сети не лечится (см. CLAUDE.md)." >&2
        exit 1
    fi
    sleep 5
done
echo "SSH отвечает."

say "2. Сервисы, аптайм, память"
ssh "$HOST" 'uptime; echo; free -m | head -2; echo; systemctl is-active KlipperScreen klipper moonraker'

say "3. Был ли OOM в ПРОШЛОЙ загрузке (то, из-за чего машина висла)"
# Может быть пусто, если журнал уже провернулся: у journald тут стоит жёсткий
# лимит, поэтому и лежит scripts/harden-journald.sh (его надо применить отдельно).
ssh "$HOST" "journalctl -b -1 -p err --no-pager 2>/dev/null | grep -iE 'out of memory|oom-kill|oom_reaper' | tail -5 || true"
echo "(пусто = следов OOM в журнале нет либо журнал уже затёрт)"

say "4. Локальные правки KlipperScreen на месте?"
ssh "$HOST" 'cd /home/ultra/KlipperScreen && git diff --stat | tail -3; echo; ls -1 ks_includes/daynight_sun.py styles/big-font/style.css styles/big-font-light/style.css 2>&1'

say "5. День/ночь: расчёт и расписание"
ssh "$HOST" "cd /home/ultra/KlipperScreen && python3 -c \"
import sys; sys.path.insert(0,'.')
from ks_includes import daynight_sun as d
import datetime
lat, lon = 55.7558, 37.6173
print('сейчас день:', d.is_daytime(None, lat, lon), '| локальное время:', datetime.datetime.now().strftime('%H:%M'))
print(d.sun_times(None, lat, lon).describe())
\"" 2>&1 | head -5
echo "-- последние записи переключателя в логе --"
ssh "$HOST" "grep daynight /home/ultra/printer_data/logs/KlipperScreen.log 2>/dev/null | tail -3 || true"

say "6. Скриншот главного экрана"
ssh "$HOST" 'su - ultra -c "DISPLAY=:0 xdotool key Escape" 2>/dev/null; sleep 1; su - ultra -c "DISPLAY=:0 scrot -o /tmp/verify-display.png"'
scp -q "$HOST:/tmp/verify-display.png" "$OUT" && echo "Сохранено: $OUT"
ssh "$HOST" 'rm -f /tmp/verify-display.png'

say "Готово"
echo "Скриншот: $OUT  (увеличивать/анализировать ЛОКАЛЬНО, не на принтере)"
