#!/usr/bin/env bash
# Ручная синхронизация конфигов из printer-configs/ на принтер. Запускать самому,
# стоя рядом с принтером — НЕ предназначен для автозапуска по CI/merge
# (стол греется без полной защиты по 220В, см. docs/printer-status.md).
#
# Конфиги правим ТОЛЬКО тут, в репозитории (см. CLAUDE.md) — не на самом принтере
# по SSH. Этот скрипт — единственный путь донести правки до Klipper.
set -euo pipefail

HOST="${PRINTER_HOST:-ultra@192.168.11.160}"
REMOTE_DIR="printer_data/config"
LOCAL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../printer-configs" && pwd)"
FILES=(printer.cfg moonraker.conf crowsnest.conf autotune_tmc.cfg KlipperScreen.conf mainsail.cfg)

echo "== Проверяю связь с принтером =="
if ! curl -s -m 5 "http://192.168.11.160:7125/printer/info" | grep -q '"state"'; then
    echo "Принтер не отвечает на :7125 — прерываю. Проверь SSH/HTTP руками." >&2
    exit 1
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
read -r -p "Продолжить? Стол/хотэнд должны быть под присмотром. [y/N] " answer
if [ "$answer" != "y" ] && [ "$answer" != "Y" ]; then
    echo "Отменено."
    exit 0
fi

TS="$(ssh "$HOST" "date +%Y%m%d%H%M%S")"
for f in "${CHANGED[@]}"; do
    echo "-> бэкап $f -> $f.bak.$TS"
    ssh "$HOST" "cp $REMOTE_DIR/$f $REMOTE_DIR/$f.bak.$TS 2>/dev/null || true"
    echo "-> заливаю $f"
    scp "$LOCAL_DIR/$f" "$HOST:$REMOTE_DIR/$f"
done

echo
echo "== Перезапускаю Klipper и жду 'ready' =="
curl -s -X POST "http://192.168.11.160:7125/printer/restart" >/dev/null

ok=0
for i in $(seq 1 15); do
    sleep 2
    state="$(curl -s -m 5 "http://192.168.11.160:7125/printer/info" | grep -o '"state":"[a-z]*"' || true)"
    echo "  [$i/15] $state"
    if echo "$state" | grep -q ready; then
        ok=1
        break
    fi
done

if [ "$ok" -eq 1 ]; then
    echo "OK: printer/info вернул ready."
else
    echo "ВНИМАНИЕ: ready не дождались за 30с — проверь klippy.log и Mainsail руками, не оставляй стол без присмотра." >&2
    exit 1
fi
