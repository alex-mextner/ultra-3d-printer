#!/usr/bin/env bash
# Устанавливает экран (ILI9341, SPI0) + 5 кнопок навигации на Orange Pi.
# Идемпотентен - безопасно перезапускать. Запускать КАК ROOT прямо на Orange Pi
# из локального чекаута репозитория (склонировать репозиторий на сам принтер
# и запустить `bash scripts/install-display.sh` локально от root).
#
# ВАЖНО: `ssh root@<ip> 'bash -s' < scripts/install-display.sh` НЕ работает -
# при запуске скрипта со stdin $BASH_SOURCE пуст, путь к orangepi-display/ не
# резолвится, а сам каталог orangepi-display/ (оверлеи, конфиги, демон) при
# таком пайпе на удалённую машину вообще не попадает - улетает только текст
# самого скрипта. Только вариант "склонировать репозиторий и запустить
# локально" реально работает (см. проверку DISPLAY_DIR ниже - она поймает
# и явно объяснит эту ошибку, если всё-таки попробовать через stdin).
#
# Что делает:
#   1. Ставит пакеты (python3-evdev, unclutter-xfixes, scrot, xdotool).
#   2. Компилирует и устанавливает оба device-tree оверлея (экран + кнопки).
#   3. Правит /boot/armbianEnv.txt (убирает spi-spidev, включает user_overlays=).
#   4. Ставит Xorg-конфиг (заставляет X рисовать в ili9341, а не в HDMI).
#   5. Ставит systemd override для KlipperScreen (лимит рестартов, снятие
#      подсветки/blank после VT-переключения, скрытие курсора).
#   6. Ставит и включает демон nav-buttons-remap (long-press left -> Escape).
#
# Не трогает printer-configs/ (printer.cfg, KlipperScreen.conf и т.д.) -
# это отдельный путь через scripts/deploy.sh, см. CLAUDE.md.
#
# После первого запуска нужен `reboot`, чтобы применились device-tree оверлеи.
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
    echo "Нужен root (запускать через ssh root@... или sudo)." >&2
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-}")" && pwd)"
DISPLAY_DIR_CANDIDATE="$SCRIPT_DIR/../orangepi-display"
if [ ! -f "$DISPLAY_DIR_CANDIDATE/overlays/sun8i-h3-ili9341.dts" ]; then
    echo "Не нахожу $DISPLAY_DIR_CANDIDATE - похоже, скрипт запущен не из" >&2
    echo "локального чекаута репозитория (например, через" >&2
    echo "'ssh root@... \"bash -s\" < scripts/install-display.sh' - так делать" >&2
    echo "нельзя, orangepi-display/ по такому пайпу на удалённую машину не" >&2
    echo "попадает). Склонируй репозиторий на этой машине и запусти отсюда:" >&2
    echo "  bash scripts/install-display.sh" >&2
    exit 1
fi
DISPLAY_DIR="$(cd "$DISPLAY_DIR_CANDIDATE" && pwd)"

echo "== 1. Пакеты =="
apt-get update -qq
apt-get install -y python3-evdev unclutter-xfixes scrot xdotool

echo "== 2. Оверлеи (dtc compile) =="
WORKDIR="$(mktemp -d)"
trap 'rm -rf "$WORKDIR"' EXIT

dtc -@ -I dts -O dtb -o "$WORKDIR/sun8i-h3-ili9341.dtbo" "$DISPLAY_DIR/overlays/sun8i-h3-ili9341.dts"
dtc -@ -I dts -O dtb -o "$WORKDIR/sun8i-h3-nav-buttons.dtbo" "$DISPLAY_DIR/overlays/sun8i-h3-nav-buttons.dts"

mkdir -p /boot/overlay-user
cp "$WORKDIR/sun8i-h3-ili9341.dtbo" "$WORKDIR/sun8i-h3-nav-buttons.dtbo" /boot/overlay-user/
echo "  Оверлеи скомпилированы и скопированы в /boot/overlay-user/"

echo "== 3. /boot/armbianEnv.txt =="
ENVFILE=/boot/armbianEnv.txt
TS="$(date +%Y%m%d%H%M%S)"
cp "$ENVFILE" "$ENVFILE.bak.$TS"

# Убираем spi-spidev из overlays= (экран сам займёт SPI0 CS0) - идемпотентно.
if grep -q '^overlays=.*spi-spidev' "$ENVFILE"; then
    sed -i 's/^overlays=spi-spidev$/overlays=/' "$ENVFILE"
    sed -i 's/ spi-spidev//; s/spi-spidev //' "$ENVFILE"
fi
sed -i '/^param_spidev_spi_bus=/d' "$ENVFILE"

if grep -q '^user_overlays=' "$ENVFILE"; then
    sed -i 's/^user_overlays=.*/user_overlays=sun8i-h3-ili9341 sun8i-h3-nav-buttons/' "$ENVFILE"
else
    echo 'user_overlays=sun8i-h3-ili9341 sun8i-h3-nav-buttons' >> "$ENVFILE"
fi
echo "  Бэкап: $ENVFILE.bak.$TS"

echo "== 4. Xorg config =="
mkdir -p /etc/X11/xorg.conf.d
cp "$DISPLAY_DIR/xorg.conf.d/20-ili9341.conf" /etc/X11/xorg.conf.d/20-ili9341.conf

echo "== 5. Скрипты в /usr/local/bin =="
cp "$DISPLAY_DIR/klipperscreen-cursor-hide.sh" /usr/local/bin/klipperscreen-cursor-hide.sh
cp "$DISPLAY_DIR/nav-buttons-remap.py" /usr/local/bin/nav-buttons-remap.py
chmod +x /usr/local/bin/klipperscreen-cursor-hide.sh /usr/local/bin/nav-buttons-remap.py

echo "== 6. systemd: KlipperScreen override + nav-buttons-remap.service =="
mkdir -p /etc/systemd/system/KlipperScreen.service.d
cp "$DISPLAY_DIR/systemd/KlipperScreen.service.d-override.conf" /etc/systemd/system/KlipperScreen.service.d/override.conf
cp "$DISPLAY_DIR/systemd/nav-buttons-remap.service" /etc/systemd/system/nav-buttons-remap.service

systemctl daemon-reload
systemctl enable nav-buttons-remap.service
# restart (not "enable --now"): --now no-ops on an already-active unit, so a
# rerun after editing nav-buttons-remap.py wouldn't actually pick up the
# change until the next reboot. restart is a no-op-safe start on first run too.
systemctl restart nav-buttons-remap.service

echo
echo "Готово. Нужна перезагрузка (sudo reboot), чтобы применились device-tree"
echo "оверлеи (экран/кнопки). KlipperScreen.conf (keyboard_navigation, theme,"
echo "font_size, show_cursor) - отдельно, через printer-configs/ + scripts/deploy.sh."
