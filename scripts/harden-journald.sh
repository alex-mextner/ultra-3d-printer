#!/usr/bin/env bash
# Делает systemd-journal persistent + увеличивает SystemMaxUse на Orange Pi.
# Идемпотентен - безопасно перезапускать. Запускать КАК ROOT прямо на Orange
# Pi из локального чекаута репозитория (склонировать репозиторий на сам
# принтер и запустить `bash scripts/harden-journald.sh` локально от root) -
# та же причина, что и у install-display.sh: скрипту нужен доступ к файлу
# orangepi-system/journald.conf рядом в дереве, а не только к себе самому,
# так что пайп через `ssh root@... "bash -s" < scripts/harden-journald.sh`
# не сработает.
#
# Контекст: GitHub issue #11 (необъяснённое ~10-минутное зависание загрузки
# на "Started wpa_supplicant..."). Причина самого зависания НЕ найдена и
# этот скрипт её не чинит - это защитная мера на случай повторения: чтобы
# в следующий раз в журнале реально осталось что анализировать. Подробности
# и находки по факту (persistent-журнал молча срывался в volatile /run,
# 20M-лимит вычищал историю меньше чем за сутки без единого краша) - см.
# docs/printer-status.md и комментарий в самом orangepi-system/journald.conf.
#
# Что делает:
#   1. Бэкапит текущий /etc/systemd/journald.conf с таймстампом.
#   2. Копирует управляемую версию из orangepi-system/journald.conf.
#   3. Перезапускает systemd-journald и просит его сразу же перенести то,
#      что успело накопиться в /run, на диск (`journalctl --flush`).
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
    echo "Нужен root (запускать через ssh root@... или sudo)." >&2
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-}")" && pwd)"
SYSTEM_DIR_CANDIDATE="$SCRIPT_DIR/../orangepi-system"
if [ ! -f "$SYSTEM_DIR_CANDIDATE/journald.conf" ]; then
    echo "Не нахожу $SYSTEM_DIR_CANDIDATE/journald.conf - похоже, скрипт" >&2
    echo "запущен не из локального чекаута репозитория (например, через" >&2
    echo "'ssh root@... \"bash -s\" < scripts/harden-journald.sh' - так делать" >&2
    echo "нельзя). Склонируй репозиторий на этой машине и запусти отсюда:" >&2
    echo "  bash scripts/harden-journald.sh" >&2
    exit 1
fi
SYSTEM_DIR="$(cd "$SYSTEM_DIR_CANDIDATE" && pwd)"

echo "== 1. Бэкап текущего /etc/systemd/journald.conf =="
TS="$(date +%Y%m%d%H%M%S)"
cp /etc/systemd/journald.conf "/etc/systemd/journald.conf.bak.$TS"
echo "  Бэкап: /etc/systemd/journald.conf.bak.$TS"

echo "== 2. Копируем управляемую версию =="
cp "$SYSTEM_DIR/journald.conf" /etc/systemd/journald.conf

echo "== 3. Перезапуск systemd-journald + перенос накопленного из /run на диск =="
systemctl restart systemd-journald
sleep 1
journalctl --flush

echo "== 4. Проверка: персистентный файл действительно ONLINE (пишется) =="
# journalctl --header без --file недостаточен как проверка: он показывает
# заголовок ОДНОГО файла на выбор самого journalctl, и после restart+flush
# может показать как /run, так и /var в зависимости от порядка обхода -
# см. обсуждение в issue #11. Проверяем конкретно персистентный файл.
PERSIST_FILE="$(ls -t /var/log.hdd/journal/*/system.journal 2>/dev/null | head -1)"
if [ -z "$PERSIST_FILE" ]; then
    echo "  ⚠ Не нашёл /var/log.hdd/journal/*/system.journal - persistent-хранилище" >&2
    echo "    не создалось, проверять руками." >&2
    exit 1
fi
if journalctl --file="$PERSIST_FILE" --header 2>&1 | grep -q '^State: ONLINE'; then
    echo "  OK: $PERSIST_FILE — State: ONLINE (пишется прямо сейчас)."
else
    echo "  ⚠ $PERSIST_FILE — НЕ ONLINE. Storage=persistent не подхватился с первого" >&2
    echo "    раза (см. issue #11 - точный триггер отката на volatile не до конца" >&2
    echo "    восстановлен). Проверить руками:" >&2
    echo "    journalctl --file=$PERSIST_FILE --header" >&2
    exit 1
fi

echo
echo "Готово."
