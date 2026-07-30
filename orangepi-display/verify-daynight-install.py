#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Проверка УСТАНОВЛЕННОЙ на принтере копии ks_includes/daynight_sun.py.

Чем отличается от `verify-daynight-sun.py` (фаза 1): тот проверяет алгоритм как
таковой (86 проверок против эталона USNO и против независимой закрытой формулы)
и запускается по любой копии файла. Этот - маленький и про КОНКРЕТНУЮ живую
установку: он смотрит именно на тот файл, который импортирует KlipperScreen, и
первым делом убеждается, что в нём не остался временный отладочный хук.

Зачем отдельная проверка на хук. Чтобы проверить смену темы, не дожидаясь
настоящего заката, в `solar_altitude()` временно вставляется ранний
`return +45.0` при наличии `/tmp/force_day` и `return -45.0` при
`/tmp/force_night`. Пока такой хук стоит, модуль отдаёт "полярную ночь" (или
"полярный день") для Москвы на ЛЮБУЮ дату, а `is_daytime()` залипает в одном
значении - и это неотличимо от полного отказа расчёта, если не знать про хук.
Ровно такая ложная тревога случилась 2026-07-31. Поэтому проверка снятия хука
идёт ПЕРВОЙ и валит скрипт, а не просто печатает предупреждение.

Запуск на принтере (системным python, не из venv KlipperScreen - gi не нужен):

    scp orangepi-display/verify-daynight-install.py root@192.168.11.160:/tmp/
    ssh root@192.168.11.160 '/usr/bin/python3 /tmp/verify-daynight-install.py'

Ничего не пишет и не перезапускает - только читает.
"""
import datetime
import os
import sys

KS_ROOT = os.environ.get("KS_ROOT", "/home/ultra/KlipperScreen")
MODULE = os.path.join(KS_ROOT, "ks_includes", "daynight_sun.py")

# Москва - ПРЕДПОЛОЖЕНИЕ по часовому поясу устройства, с пользователем не
# подтверждалось (см. README). Для проверки самой математики важно только то,
# что это середина широт, а не заполярье.
LAT, LON = 55.7558, 37.6173

# Опубликованные значения (USNO, целые минуты) для Москвы. USNO округляет до
# минуты, так что ~0.5 мин запаса заложено в самом эталоне.
EXPECTED = {
    "2026-07-31": ("04:33", "20:38"),
    "2026-06-21": ("03:45", "21:18"),
    "2026-12-21": ("08:57", "15:58"),
    "2026-03-20": ("06:32", "18:43"),
}
TOLERANCE_MIN = 2.0

sys.path.insert(0, KS_ROOT)

src = open(MODULE, encoding="utf-8").read()
for needle in ("force_day", "force_night", "TEMPORARY VERIFICATION HOOK"):
    assert needle not in src, (
        f"ОТЛАДОЧНЫЙ ХУК ВСЁ ЕЩЁ В ФАЙЛЕ: {needle!r} найден в {MODULE}.\n"
        "Пока он там, любая проверка модуля меряет хук, а не расчёт. "
        "Переставь чистую копию из orangepi-display/ks_includes/daynight_sun.py."
    )
leftovers = [p for p in ("/tmp/force_day", "/tmp/force_night") if os.path.exists(p)]
print(f"хук снят: ни force_day/force_night, ни маркера хука в {MODULE}")
print(f"файлы-сентинелы на диске: {leftovers or 'нет'}")
print()

from ks_includes import daynight_sun as d  # noqa: E402

lengths = {}
worst = 0.0
for datestr in sorted(EXPECTED):
    year, month, day = map(int, datestr.split("-"))
    st = d.sun_times(datetime.date(year, month, day), LAT, LON)
    print(datestr, "|", st.describe())
    assert not st.polar_day and not st.polar_night, (
        f"{datestr}: полярный день/ночь на широте {LAT} - физически невозможно "
        "(полярный круг начинается с 66.5)"
    )
    assert st.sunrise and st.sunset, f"{datestr}: нет восхода или заката"
    lengths[datestr] = st.day_length
    for label, got, exp in (
        ("восход", st.sunrise, EXPECTED[datestr][0]),
        ("закат", st.sunset, EXPECTED[datestr][1]),
    ):
        exp_h, exp_m = map(int, exp.split(":"))
        delta = abs(
            (got.hour * 60 + got.minute + got.second / 60.0) - (exp_h * 60 + exp_m)
        )
        worst = max(worst, delta)
        assert delta <= TOLERANCE_MIN, (
            f"{datestr} {label}: получено {got:%H:%M:%S}, опубликовано {exp}, "
            f"расхождение {delta:.2f} мин > {TOLERANCE_MIN}"
        )

now = datetime.datetime.now()
print()
print(f"сейчас {now:%H:%M:%S}: is_daytime={d.is_daytime(None, LAT, LON)}, "
      f"высота Солнца {d.solar_altitude(now, LAT, LON):+.2f} (порог {d.HORIZON_ALTITUDE})")
nxt = d.next_transition(None, LAT, LON)
print(f"следующая смена темы: {nxt.isoformat(timespec='seconds') if nxt else 'не в ближайшие 190 суток'}")
print()

# Самая дешёвая проверка на перепутанные радианы/градусы и знаки: если первичная
# solar_altitude() сломана, всё построенное над ней сломано тоже.
noon = d.solar_altitude(datetime.datetime(2026, 6, 21, 12, 36), LAT, LON)
midnight = d.solar_altitude(datetime.datetime(2026, 6, 21, 0, 36), LAT, LON)
print(f"2026-06-21 высота в солнечный полдень:  {noon:+.2f} (ожидается ~ +57)")
print(f"2026-06-21 высота в солнечную полночь:  {midnight:+.2f} (ожидается ~ -11)")
assert 55.0 < noon < 59.0, f"высота в полдень {noon:+.2f} вне ожидаемого диапазона"
assert -13.0 < midnight < -9.0, f"высота в полночь {midnight:+.2f} вне ожидаемого диапазона"

longest = max(lengths, key=lambda k: lengths[k])
shortest = min(lengths, key=lambda k: lengths[k])
print(f"самый длинный день:  {longest} ({lengths[longest]})")
print(f"самый короткий день: {shortest} ({lengths[shortest]})")
assert longest == "2026-06-21", "самый длинный день не на июньском солнцестоянии"
assert shortest == "2026-12-21", "самый короткий день не на декабрьском солнцестоянии"

print()
print(f"ВСЕ ПРОВЕРКИ ПРОЙДЕНЫ - худшее отклонение от опубликованных значений {worst:.2f} мин")
