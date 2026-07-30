# Экран (ILI9341) + 5 кнопок навигации на Orange Pi

Артефакты, собранные в сессии 2026-07-30 при подключении 2.2" SPI TFT (ILI9341,
240x320, физически развёрнут на 90°) и 5-кнопочного модуля навигации к Orange
Pi One. Полная распиновка и история решений - в `docs/printer-status.md`,
раздел "ПЛАН: экран + 5 кнопок навигации".

Это **системный** (OS-уровня) слой, отдельный от `printer-configs/` (который
только про Klipper/Moonraker/Mainsail/crowsnest/KlipperScreen.conf и
деплоится через `scripts/deploy.sh`). Всё в этой папке ставится один раз
через `scripts/install-display.sh` и не меняется при обычных правках
принтера.

## Структура

- `overlays/*.dts` - device-tree оверлеи (текстовый исходник, НЕ бинарь).
  Компилируются в `.dtbo` на месте, при установке (`dtc`, есть в Armbian из
  коробки). Бинарные `.dtbo` в репозиторий не кладём принципиально.
- `xorg.conf.d/20-ili9341.conf` - заставляет X рисовать в DRM-карточку
  ILI9341 (`/dev/dri/by-path/platform-1c68000.spi-cs-0-card`, стабильный путь,
  НЕ `/dev/dri/cardN` - номер карточки не гарантированно одинаков между
  загрузками, см. ниже), а не в HDMI-контроллер SoC (который у него есть
  физически, просто не подключен - X иначе пытается использовать оба сразу
  и падает).
- `systemd/KlipperScreen.service.d-override.conf` - лимит рестартов (3 подряд)
  + принудительное снятие blank/подсветки после переключения VT + запуск
  скрытия курсора.
- `systemd/nav-buttons-remap.service` + `nav-buttons-remap.py` - демон на
  python-evdev: перехватывает физические кнопки, долгое удержание left шлёт
  Escape (у KlipperScreen это "назад/домой" - без этого демона из панелей
  вроде notifications физически нет выхода, только стрелки+Enter).
- `klipperscreen-cursor-hide.sh` - запускает `unclutter-xfixes`, раз мышки
  физически нет и не будет.
- `ks_includes/daynight_sun.py` - оффлайновый расчёт восхода/заката для
  автопереключения светлой и тёмной темы. Имя каталога повторяет каталог
  назначения: файл ставится в `/home/ultra/KlipperScreen/ks_includes/`, откуда
  импортируется как `from ks_includes.daynight_sun import ...`. Подробности -
  раздел ниже.
- `verify-daynight-sun.py` - проверка этого расчёта против внешнего эталона.
  НЕ ставится на принтер, запускается вручную.
- `klipperscreen-patches/` - патчи поверх самого чекаута KlipperScreen
  (`/home/ultra/KlipperScreen`), не входящие в printer-configs/. Такие патчи
  **слетают при обновлении KlipperScreen** (`git pull`/самообновление) - надо
  переприменять руками, см. README внутри папки.

## Установка на новую систему

Склонировать репозиторий прямо на Orange Pi и запустить
`scripts/install-display.sh` локально от root. Скрипт идемпотентен - можно
перезапускать. После первого запуска нужен `reboot` (device-tree оверлеи
применяются только при загрузке).

(`ssh root@<ip> 'bash -s' < scripts/install-display.sh` НЕ работает - скрипт
резолвит путь к orangepi-display/ через собственный путь на диске, а при
запуске со stdin такого пути нет, да и сам каталог orangepi-display/ по
такому пайпу на удалённую машину не попадает - улетает только текст скрипта.)

`KlipperScreen.conf` (`keyboard_navigation`, `theme`, `font_size`,
`show_cursor`) - НЕ часть этого скрипта, это `printer-configs/` +
`scripts/deploy.sh`, как и остальные Klipper-конфиги.

`ks_includes/daynight_sun.py` тоже НЕ часть этого скрипта: он ставится не в
систему, а внутрь чекаута KlipperScreen (как и `klipperscreen-patches/`, и так
же слетает при обновлении KlipperScreen), поэтому его копирует фаза
переключения тем, а не `install-display.sh`. `verify-daynight-sun.py` на
принтер не ставится вообще - это тест, запускается вручную.

(Скрипт копирует строго перечисленные пути, никаких `*.py`/`cp -r` по каталогу,
так что новые файлы он просто не видит и никуда не утащит.)

## Смена темы по солнцу: расчётный слой (`ks_includes/daynight_sun.py`)

Запрос пользователя: "переключение на темную тему с закатом сделай, прикольная
идея". Первая фаза - только математика, живого состояния устройства не трогает.

**Вторая фаза сделана и проверена живьём (2026-07-31)** - см.
`klipperscreen-patches/daynight-theme-switch.patch` и его разбор в
`klipperscreen-patches/README.md`: там и таймер `GLib.timeout_add_seconds(300)`,
и вызов `change_theme()`, и опции `[main]` (`auto_dark_theme`, `light_theme`,
`dark_theme`, `latitude`, `longitude`), и обязательный патч белого списка в
`ks_includes/config.py`, о котором предупреждает раздел ниже.

Считается локально, без сети: алгоритм NOAA на чистом stdlib (math + datetime +
zoneinfo). Интернета на принтере может не быть, а оффлайновый расчёт вдобавок
детерминирован и потому проверяем тестами, чего про поход в чужой API не
скажешь.

Публичный API (полные сигнатуры и оговорки - в docstring-ах модуля):

```python
is_daytime(now=None, latitude=DEFAULT_LATITUDE, longitude=DEFAULT_LONGITUDE, tz=None) -> bool
sun_times(day=None, latitude=..., longitude=..., tz=None) -> SunTimes
next_transition(now=None, latitude=..., longitude=..., tz=None, max_days=190, step_minutes=30) -> datetime | None
load_location(config_path=None) -> (latitude, longitude)
solar_altitude(moment, latitude=..., longitude=..., tz=None) -> float
local_timezone() -> tzinfo
```

Есть и CLI для ручной проверки прямо на принтере:
`/usr/bin/python3 ks_includes/daynight_sun.py [--lat --lon --date --config]`.

### ПРЕДУСЛОВИЕ (выполнено во второй фазе): без патча config.py киоск не поднимется

`load_location()` читает `latitude`/`longitude` из секции `[main]` файла
KlipperScreen.conf. Но KlipperScreen валидирует конфиг по белому списку опций,
и на незнакомую опцию в `[main]` кладёт сообщение в `self.errors`, после чего
`screen.py` показывает модалку "Invalid config file" и делает `return` **до**
`self.base_panel.activate()`:

```python
# screen.py:224-226
if self._config.errors:
    self.show_error_modal("Invalid config file", self._config.get_errors())
    return
```

То есть это не косметическое предупреждение в логе, а неподнявшийся
интерфейс на экране 320x240. Прежде чем прописывать координаты в конфиг, надо
добавить обе опции в белый список `numbers` секции "main" в
`ks_includes/config.py` (`is_float` отрицательные значения принимает, для
южного/западного полушария правок не нужно):

```diff
                 numbers = (
                     "job_complete_timeout",
                     "job_error_timeout",
+                    "latitude",
+                    "longitude",
                     "move_speed_xy",
```

Патч в `klipperscreen-patches/` в первой фазе намеренно НЕ заводился: у той
папки контракт "root cause + диф + подтверждение живьём", а подтвердить живьём
было нечем - фаза устройства не касалась. **Во второй фазе заведён:**
`daynight-theme-switch.patch` содержит этот хунк вместе с тремя другими
(`auto_dark_theme` в `bools`, `light_theme`/`dark_theme` в `strs`) и правкой
`screen.py`. Предупреждение подтвердилось буквально: на непропатченном
`config.py` валидатор выдал ровно пять ошибок "Option ... not recognized",
на пропатченном - `valid: True errors: []` (проверено на устройстве ДО
раскатки конфига, чтобы киоск не лёг).

### Координаты по умолчанию - предположение, а не факт

55.7558 N, 37.6173 E (Москва) выведены из часового пояса устройства
(`timedatectl` -> `Europe/Moscow`). **С пользователем это не согласовывалось.**
Принтер может стоять где угодно в пределах MSK - от Петрозаводска до
Краснодара, и это не одно и то же: на широте Петрозаводска летний день длиннее
московского почти на час. Поэтому координаты и вынесены в конфиг.

Разброс невелик, но заметен: сдвиг на 1 градус долготы двигает восход и закат
на 4 минуты, на 1 градус широты - до нескольких минут в зависимости от сезона.

### Как проверялось

`verify-daynight-sun.py`, 86 проверок, все зелёные. Запускался на самом Orange
Pi (`/usr/bin/python3`, Python 3.13.5) - то есть в целевом рантайме, а не на
машине разработчика:

```
scp orangepi-display/ks_includes/daynight_sun.py root@<ip>:/tmp/v/ks_includes/
scp orangepi-display/verify-daynight-sun.py     root@<ip>:/tmp/v/
ssh root@<ip> 'cd /tmp/v && /usr/bin/python3 verify-daynight-sun.py'
```

Внешний эталон - **US Naval Observatory** (`aa.usno.navy.mil/api/rstt/oneday`),
полная эфемерида, независимая от реализованного здесь приближения NOAA.
Максимальное расхождение по 8 контрольным точкам (оба солнцестояния, оба
равноденствия, Москва + Сидней + Кито) - **0.47 минуты**, при том что сам USNO
округляет ответ до минуты. Полярный день и полярная ночь на Шпицбергене
совпали с вердиктом USNO "Object continuously above/below the Horizon".

Второй, внутренний эталон - классическая закрытая формула NOAA через `acos`
часового угла (другая ветка вывода, ловит ошибки знака и единиц). На тех же
точках она даёт максимум 1.04 минуты от USNO, то есть реализованная бисекция по
высоте Солнца заметно точнее - как и ожидалось, потому что она берёт склонение
в момент самого события, а не в полдень.

Что поймали тесты (обе ошибки исправлены до коммита):

- **`day_length` ошибался ровно на час в сутки перевода стрелок.** У двух
  осведомлённых datetime с ОДНИМ И ТЕМ ЖЕ объектом `tzinfo` Python вычитает
  стенное время без поправки на переход ("the common tzinfo attribute is
  ignored", docs/datetime). Лечится вычитанием через UTC.
- **Восход округлялся на секунду позже нужного на 24 сутках из 365.**
  Округления вверх недостаточно: бисекция знает момент с точностью ±0.25 с и,
  промахнувшись через границу целой секунды, уводила ceil на секунду вперёд.
  Заменено явным перебором окна ±2 с.

## Важные грабли (если придётся переустанавливать на другой Orange Pi)

- **Нумерация `/dev/dri/cardN` и `/boot/dtb-*/overlay/` НЕ стабильна между
  загрузками** - между двумя перезагрузками в этой же сессии ili9341 был то
  `card1`, то `card2` (лишний GPU-рендер-нод `lima` менял место). Отсюда
  `by-path` в xorg-конфиге, а не номер карточки.
- **X по умолчанию пытается использовать ВСЕ обнаруженные DRM-устройства
  разом** (multi-GPU/zaphod-режим) и падает с "Cannot run in framebuffer
  mode... busIDs" на платформенных (не PCI) устройствах без явного
  `Option "AutoAddGPU" "false"` в `ServerFlags`.
- **`ExecStartPost` в systemd-юните KlipperScreen выполняется от `User=ultra`**
  (как весь юнit), а нужные sysfs-файлы (`fb0/blank`, `bl_power`) - root-only.
  Решение - префикс `+` перед конкретной командой (не перед всем юнитом).
- **`sudo` по SSH через `!`-команду харнеса не работает** (нет TTY для
  пароля) - root-шаги либо через `ssh root@...` напрямую (если ключ
  настроен), либо пользователь запускает сам в своём терминале, не через `!`.
