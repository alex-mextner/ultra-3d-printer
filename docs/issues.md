# Открытые задачи

Плоский список того, что известно как незакрытое, но не является активным
статусом на сейчас (для этого - `printer-status.md`). Не GitHub Issues (`gh`
не установлен/не настроен в этом окружении, в проекте нет GitHub Issues
workflow) - обычный markdown, как всё остальное в `docs/`.

## Экран/KlipperScreen

- [x] Экран гас навсегда через ~1ч простоя (DPMS Suspend:3600 по умолчанию у
  X, KlipperScreen не переоткрывает подсветку) - обнаружено живьём 2026-07-30
  (пользователь вернулся к чёрному экрану). Исправлено:
  `orangepi-display/xorg.conf.d/20-ili9341.conf` теперь явно глушит
  Standby/Suspend/Off/BlankTime в ServerFlags. Задеплоено и проверено
  переживающим `systemctl restart KlipperScreen`.
- [x] Шрифт/иконки - `font_size: medium` + тема `big-font` (не `max`, см.
  комментарий в `printer-configs/KlipperScreen.conf`). Подтверждено живым
  скриншотом: подписи Extruder/Heater Bed/OrangePi CPU видны полностью, без
  скролбара, иконки на месте.
- [x] Переполнение цифровой клавиатуры вправо за край экрана -
  `orangepi-display/klipperscreen-patches/keypad-width-overflow.patch`,
  снят с живой правки на принтере, README рядом объясняет как переприменять
  после обновления KlipperScreen.

## Скрипты

- [ ] `scripts/deploy.sh`: `read -r -p "Продолжить?"` виснет/молча падает при
  неинтерактивном запуске (`echo y | bash scripts/deploy.sh`) - `ssh`-вызовы
  внутри собственного diff-цикла скрипта потребляют переданный в pipe stdin
  раньше, чем до него доходит `read`. Не чинилось в этой сессии - деплои
  делались вручную (backup + scp + chown) в обход самого скрипта. Чинить:
  либо `read ... < /dev/tty`, либо `ssh -n`/`</dev/null` на diff-вызовах
  внутри скрипта, чтобы не есть stdin родителя.

## UX judge-panel (Fable)

_Заполняется автоматически после прогона judge-panel воркфлоу
(`Goal set: dynamic workflow + fable` от 2026-07-30) - см. ниже в этом же
файле после соответствующего раздела, если он есть, иначе воркфлоу ничего
серьёзного не нашёл._
