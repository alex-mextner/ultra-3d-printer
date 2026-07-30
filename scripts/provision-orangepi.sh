#!/usr/bin/env bash
# Полная настройка чистого Orange Pi (Armbian) под этот принтер с нуля:
# Klipper/Moonraker/Mainsail/crowsnest/KlipperScreen через KIAUH, конфиги
# принтера, экран + кнопки. Запускать КАК ROOT из локального чекаута
# репозитория прямо на Orange Pi (склонировать репозиторий на принтер и
# запустить `bash scripts/provision-orangepi.sh` локально).
#
# НЕ запускать через `ssh root@<ip> 'bash -s' < scripts/provision-orangepi.sh`:
# 1) install-display.sh (шаг 3) не находит orangepi-display/ при запуске со
#    stdin (см. проверку в самом install-display.sh); 2) pause() ниже читает
#    Enter из stdin, а при таком пайпе stdin - это ОСТАВШИЕСЯ СТРОКИ САМОГО
#    СКРИПТА, так что пауза не подождёт человека, а молча схлопнется.
#
# ВАЖНО про KIAUH: у него НЕТ неинтерактивного/CLI режима (проверено по
# исходникам/README проекта dw-0/kiauh) - это whiptail-меню, флагов для
# headless-установки не существует. Этот скрипт НЕ притворяется, что умеет
# прогнать KIAUH без участия человека - он ставит паузы с точными пунктами
# меню, которые нужно выбрать руками, и продолжает автоматически после
# каждой паузы. Всё, что скриптуется целиком (пакеты, экран/кнопки, деплой
# printer-configs/) - делается без вопросов.
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
    echo "Нужен root." >&2
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

pause() {
    echo
    echo "=================================================================="
    echo "$1"
    echo "=================================================================="
    read -r -p "Нажми Enter, когда сделано... " _ < /dev/tty
}

echo "== 1. Базовые пакеты =="
apt-get update -qq
apt-get install -y git curl python3 python3-pip

echo
echo "== 2. KIAUH (Klipper Install And Update Helper) =="
# ВАЖНО: клонировать и запускать KIAUH нужно от ultra, НЕ от root - весь стек
# (klipper.service User=ultra, printer_data/config, deploy.sh) живёт в
# /home/ultra/. Этот скрипт сам выполняется от root (нужен для apt-get и
# install-display.sh), поэтому $HOME здесь - /root, и клонировать kiauh в
# $HOME/kiauh значило бы поставить весь стек не туда, откуда его читает
# deploy.sh (ultra@...:printer_data/config). Явно целимся в /home/ultra.
KIAUH_DIR=/home/ultra/kiauh
if [ ! -d "$KIAUH_DIR" ]; then
    git clone https://github.com/dw-0/kiauh.git "$KIAUH_DIR"
    chown -R ultra:ultra "$KIAUH_DIR"
fi

pause "Запусти сейчас САМ, ОТ ИМЕНИ ultra (не root!) - в отдельном терминале
\`ssh ultra@<ip-этой-машины>\` (или \`su - ultra\` прямо тут), затем
интерактивно (у KIAUH нет неинтерактивного режима): \`~/kiauh/kiauh.sh\`
В меню установи по очереди: Klipper -> Moonraker -> Mainsail -> Crowsnest ->
KlipperScreen. Дефолтные ответы на все вопросы подходят, кроме тех, что
касаются конкретно этой машины (порт MCU и т.п. - см. docs/printer-status.md,
раздел 'Железо'). Когда весь стек установлен и \`systemctl status klipper\`
живой - возвращайся сюда."

echo
echo "== 3. Экран (ILI9341) + 5 кнопок навигации =="
bash "$SCRIPT_DIR/install-display.sh"

pause "install-display.sh отработал. Сейчас нужна перезагрузка, чтобы
применились device-tree оверлеи экрана/кнопок: выполни \`reboot\` (сам, не
через автоматику - это возврат SSH-сессии). После того как машина снова
на связи - возвращайся сюда."

echo
echo "== 4. Конфиги принтера (printer.cfg, moonraker.conf, ...) =="
echo "Это отдельный путь - деплоится с РАБОЧЕЙ станции (не отсюда, не с самого"
echo "Orange Pi), через scripts/deploy.sh, у которого свой интерактивный"
echo "confirm-prompt (diff + y/N). На рабочей машине, в корне репозитория:"
echo
echo "    bash scripts/deploy.sh"
echo
echo "Готово. Дальше - см. 'С ЧЕГО НАЧАТЬ МИНИ-СЕССИЮ' в docs/printer-status.md."
