#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Проверка daynight_sun.py.

Эталон 1 (внешний, независимый): US Naval Observatory Astronomical Applications
API, https://aa.usno.navy.mil/api/rstt/oneday - полная эфемерида, а НЕ
приближение NOAA, которое реализовано в модуле. Значения снимались 2026-07-31,
округлены USNO до минуты.

Эталон 2 (внутренний, независимый вывод): классическая закрытая формула NOAA
через acos часового угла - другая ветка математики, чем бисекция по высоте.
"""
import math
import os
import sys
import tempfile
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

_HERE = os.path.dirname(os.path.abspath(__file__))
# Модуль лежит в ks_includes/ рядом (в репозитории) либо прямо рядом (если
# оба файла скопированы в /tmp на принтере).
sys.path.insert(0, os.path.join(_HERE, "ks_includes"))
sys.path.insert(0, _HERE)
import daynight_sun as S

FAILURES = []
CHECKS = 0


def check(ok, label, detail=""):
    global CHECKS
    CHECKS += 1
    if not ok:
        FAILURES.append(f"{label} :: {detail}")
    return ok


def hdr(title):
    print()
    print("=" * 78)
    print(title)
    print("=" * 78)


def closed_form(day, lat, lon, tz):
    """Эталон 2: классический NOAA - часовой угол через acos, склонение и
    уравнение времени взяты один раз на локальный полдень. Совсем другая ветка
    вывода, чем бисекция по высоте в модуле, поэтому ловит ошибки знака и
    единиц. Заведомо чуть грубее бисекции: за полсуток до/после полудня
    склонение успевает измениться, а закрытая форма этого не учитывает."""
    anchor = datetime.combine(day, time(12, 0), tzinfo=tz).astimezone(timezone.utc)
    jd = anchor.timestamp() / 86400.0 + 2440587.5
    decl, eot = S._solar_declination_and_eot(jd)
    cos_ha = (math.cos(math.radians(90.833))
              / (math.cos(math.radians(lat)) * math.cos(math.radians(decl)))
              - math.tan(math.radians(lat)) * math.tan(math.radians(decl)))
    if not -1.0 <= cos_ha <= 1.0:
        return None, None
    ha = math.degrees(math.acos(cos_ha))
    noon_min = 720.0 - 4.0 * lon - eot  # минуты от полуночи UTC
    out = []
    for minutes in (noon_min - 4.0 * ha, noon_min + 4.0 * ha):
        base = datetime.combine(anchor.date(), time(0, 0), tzinfo=timezone.utc)
        cand = base + timedelta(minutes=minutes)
        # выбрать сутки UTC так, чтобы событие было ближе всего к локальному полудню
        for shift in (-1, 0, 1):
            trial = cand + timedelta(days=shift)
            if abs((trial - anchor).total_seconds()) <= 12 * 3600:
                cand = trial
                break
        out.append(cand.astimezone(tz))
    return out[0], out[1]


# ---------------------------------------------------------------------------
# 1. Абсолютная точность против USNO
# ---------------------------------------------------------------------------
MOSCOW = (55.7558, 37.6173)
SYDNEY = (-33.8688, 151.2093)
QUITO = (-0.1807, -78.4678)
LONGYEARBYEN = (78.2232, 15.6267)

USNO = [
    # (метка, lat, lon, tzname, дата, восход, закат)
    ("Москва, равноденствие (март)", *MOSCOW, "Europe/Moscow", "2026-03-20", "06:32", "18:43"),
    ("Москва, солнцестояние (июнь)", *MOSCOW, "Europe/Moscow", "2026-06-21", "03:45", "21:18"),
    ("Москва, сегодня",              *MOSCOW, "Europe/Moscow", "2026-07-31", "04:33", "20:38"),
    ("Москва, равноденствие (сент)", *MOSCOW, "Europe/Moscow", "2026-09-23", "06:16", "18:26"),
    ("Москва, солнцестояние (дек)",  *MOSCOW, "Europe/Moscow", "2026-12-21", "08:57", "15:58"),
    ("Сидней, зимнее солнцестояние", *SYDNEY, "Australia/Sydney", "2026-06-21", "07:00", "16:54"),
    ("Сидней, летнее солнцестояние", *SYDNEY, "Australia/Sydney", "2026-12-21", "05:41", "20:05"),
    ("Кито (экватор), июнь",         *QUITO,  "America/Guayaquil", "2026-06-21", "06:12", "18:19"),
]

def elapsed_min(a, b):
    """Реально прошедшее время b-a в минутах.

    ЧЕРЕЗ UTC НАМЕРЕННО: у двух осведомлённых datetime с ОДНИМ И ТЕМ ЖЕ
    объектом tzinfo Python вычитает стенное время без поправки на перевод
    стрелок ("the common tzinfo attribute is ignored", docs/datetime).
    """
    return (b.astimezone(timezone.utc) - a.astimezone(timezone.utc)).total_seconds() / 60.0


hdr("1. АБСОЛЮТНАЯ ТОЧНОСТЬ: наш расчёт против эфемериды USNO")
print(f"{'локация/дата':<32} {'событие':<7} {'USNO':>6} {'наш':>9} {'дельта':>8}"
      f" | {'закр.форма':>10} {'её дельта':>10}")
print("-" * 96)
worst = 0.0
worst_cf = 0.0
for label, lat, lon, tzname, iso, u_rise, u_set in USNO:
    tz = ZoneInfo(tzname)
    day = date.fromisoformat(iso)
    st = S.sun_times(day, lat, lon, tz)
    cf_rise, cf_set = None, None
    for name, ref, got, cf in (("восход", u_rise, st.sunrise, "r"), ("закат", u_set, st.sunset, "s")):
        ref_dt = datetime.combine(day, time.fromisoformat(ref), tzinfo=tz)
        delta_min = elapsed_min(ref_dt, got)
        worst = max(worst, abs(delta_min))
        cfr, cfs = closed_form(day, lat, lon, tz)
        cf_dt = cfr if cf == "r" else cfs
        cf_delta = elapsed_min(ref_dt, cf_dt)
        worst_cf = max(worst_cf, abs(cf_delta))
        print(f"{label + ' ' + iso:<32} {name:<7} {ref:>6} {got.strftime('%H:%M:%S'):>9} "
              f"{delta_min:>+7.2f}м | {cf_dt.strftime('%H:%M:%S'):>10} {cf_delta:>+9.2f}м")
        check(abs(delta_min) <= 2.0, f"USNO {label} {name}", f"дельта {delta_min:+.2f} мин")
print(f"\nмаксимальное расхождение с USNO: наш (бисекция по высоте) {worst:.2f} мин, "
      f"классическая закрытая форма NOAA {worst_cf:.2f} мин")
print("USNO округляет до минуты, т.е. 0.5 мин неопределённости заложены в сам эталон.")
check(worst < worst_cf, "бисекция точнее закрытой формы", f"{worst:.2f} vs {worst_cf:.2f}")

hdr("1b. ПОЛЯРНЫЕ СУТКИ против USNO")
for iso, tzname, expect in (
    ("2026-06-21", "Europe/Oslo", "polar_day"),
    ("2026-12-21", "Europe/Oslo", "polar_night"),
):
    tz = ZoneInfo(tzname)
    st = S.sun_times(date.fromisoformat(iso), *LONGYEARBYEN, tz)
    got = "polar_day" if st.polar_day else ("polar_night" if st.polar_night else "обычные сутки")
    usno = ("Object continuously above the Horizon" if expect == "polar_day"
            else "Object continuously below the Horizon")
    print(f"Лонгйир {iso}: USNO='{usno}' -> наш={got}, "
          f"sunrise={st.sunrise}, sunset={st.sunset}, "
          f"max_alt={st.max_altitude:+.2f} min_alt={st.min_altitude:+.2f}")
    check(got == expect, f"полярные сутки {iso}", got)
    check(st.sunrise is None and st.sunset is None, f"полярные сутки {iso}: None", "не None")
    check(S.is_daytime(datetime.combine(date.fromisoformat(iso), time(3, 0), tzinfo=tz),
                       *LONGYEARBYEN) == (expect == "polar_day"),
          f"is_daytime в полярные сутки {iso}", "неверно")

hdr("1c. СОЛНЕЧНЫЙ ПОЛДЕНЬ: середина (восход+закат)/2 против верхней кульминации USNO")
# USNO 'Upper Transit' для Москвы 2026-06-21 = 12:31
tz = ZoneInfo("Europe/Moscow")
st = S.sun_times(date(2026, 6, 21), *MOSCOW, tz)
midpoint = st.sunrise + (st.sunset - st.sunrise) / 2
ref_transit = datetime.combine(date(2026, 6, 21), time(12, 31), tzinfo=tz)
print(f"USNO upper transit      : 12:31")
print(f"наш solar_noon          : {st.solar_noon.strftime('%H:%M:%S')} "
      f"(дельта {(st.solar_noon - ref_transit).total_seconds() / 60:+.2f} мин)")
print(f"наша середина дня       : {midpoint.strftime('%H:%M:%S')} "
      f"(дельта {(midpoint - ref_transit).total_seconds() / 60:+.2f} мин)")
print(f"полдень vs середина дня : {abs((st.solar_noon - midpoint).total_seconds()):.1f} сек")
check(abs((st.solar_noon - ref_transit).total_seconds()) <= 90, "полдень vs USNO transit", "")
check(abs((st.solar_noon - midpoint).total_seconds()) <= 60, "полдень vs середина дня", "")


# ---------------------------------------------------------------------------
hdr("2. ВТОРОЙ ОРАКУЛ: бисекция по высоте против закрытой формулы acos (весь 2026)")
for label, (lat, lon), tzname in (("Москва", MOSCOW, "Europe/Moscow"),
                                  ("Сидней", SYDNEY, "Australia/Sydney"),
                                  ("Кито", QUITO, "America/Guayaquil")):
    tz = ZoneInfo(tzname)
    worst_r = worst_s = 0.0
    d = date(2026, 1, 1)
    n = 0
    while d <= date(2026, 12, 31):
        st = S.sun_times(d, lat, lon, tz)
        cr, cs = closed_form(d, lat, lon, tz)
        if cr and st.sunrise:
            worst_r = max(worst_r, abs(elapsed_min(cr, st.sunrise) * 60))
            worst_s = max(worst_s, abs(elapsed_min(cs, st.sunset) * 60))
            n += 1
        d += timedelta(days=1)
    print(f"{label:<8} {n} суток: макс. расхождение восход {worst_r:5.1f} сек, "
          f"закат {worst_s:5.1f} сек")
    check(worst_r < 90 and worst_s < 90, f"два оракула {label}",
          f"{worst_r:.1f}/{worst_s:.1f} сек")


# ---------------------------------------------------------------------------
# 3. Инварианты за полный год
# ---------------------------------------------------------------------------
hdr("3. ИНВАРИАНТЫ ЗА 365 СУТОК (Москва 2026)")
tz = ZoneInfo("Europe/Moscow")
rows = []
d = date(2026, 1, 1)
while d <= date(2026, 12, 31):
    rows.append(S.sun_times(d, *MOSCOW, tz))
    d += timedelta(days=1)

bad_order = [r for r in rows if not (r.sunrise < r.sunset)]
print(f"суток посчитано               : {len(rows)}")
print(f"закат позже восхода           : {len(rows) - len(bad_order)}/{len(rows)}")
check(not bad_order, "закат после восхода", f"{len(bad_order)} нарушений")


def smoothness(series, label):
    """Максимальный сдвиг события ото дня ко дню. Именно этот тест ловит
    ошибку выбора суток UTC на единицу - она даёт разрыв в 4 минуты или в
    целые сутки на отдельных датах и спокойно проходит мимо пяти точечных
    сверок с эталоном."""
    max_r = max_s = 0.0
    day_r = day_s = None
    for a, b in zip(series, series[1:]):
        jr = abs(elapsed_min(a.sunrise, b.sunrise) - 1440)
        js = abs(elapsed_min(a.sunset, b.sunset) - 1440)
        if jr > max_r:
            max_r, day_r = jr, b.date
        if js > max_s:
            max_s, day_s = js, b.date
    print(f"{label}: макс. сдвиг восхода {max_r:.2f} мин ({day_r}), "
          f"заката {max_s:.2f} мин ({day_s})")
    check(max_r < 10 and max_s < 10, f"плавность день-к-дню {label}",
          f"{max_r:.2f}/{max_s:.2f} мин")


smoothness(rows, "Москва 2026, 364 перехода")

longest = max(rows, key=lambda r: r.day_length)
shortest = min(rows, key=lambda r: r.day_length)
print(f"самый длинный день            : {longest.date} = {longest.day_length} "
      f"(июньское солнцестояние 2026-06-21)")
print(f"самый короткий день           : {shortest.date} = {shortest.day_length} "
      f"(декабрьское солнцестояние 2026-12-21)")
check(abs((longest.date - date(2026, 6, 21)).days) <= 1, "макс. день у июньского солнцестояния",
      str(longest.date))
check(abs((shortest.date - date(2026, 12, 21)).days) <= 1, "мин. день у декабрьского солнцестояния",
      str(shortest.date))

# южное полушарие - зеркально
srows = []
d = date(2026, 1, 1)
while d <= date(2026, 12, 31):
    srows.append(S.sun_times(d, *SYDNEY, ZoneInfo("Australia/Sydney")))
    d += timedelta(days=1)
s_long = max(srows, key=lambda r: r.day_length)
s_short = min(srows, key=lambda r: r.day_length)
print(f"Сидней: самый длинный день    : {s_long.date} = {s_long.day_length} (ожидали декабрь)")
print(f"Сидней: самый короткий день   : {s_short.date} = {s_short.day_length} (ожидали июнь)")
check(s_long.date.month == 12 and s_short.date.month == 6, "южное полушарие зеркально",
      f"{s_long.date}/{s_short.date}")
smoothness(srows, "Сидней 2026 (с переводами стрелок)")

# ГАРАНТИЯ ОКРУГЛЕНИЯ на всех сутках сразу: восход - ПЕРВАЯ целая секунда, в
# которую уже день; закат - первая целая секунда, в которую уже ночь. Это то,
# на что опирается таймер фазы 2.
sec = timedelta(seconds=1)
print("\nгарантия округления до секунды (is_daytime(sunrise)=True и "
      "is_daytime(sunrise-1с)=False, is_daytime(sunset)=False и is_daytime(sunset-1с)=True):")
for series, coords, label in ((rows, MOSCOW, "Москва"),
                              (srows, SYDNEY, "Сидней"),
                              ([S.sun_times(date(2026, 1, 1) + timedelta(days=i), *QUITO,
                                            ZoneInfo("America/Guayaquil")) for i in range(365)],
                               QUITO, "Кито")):
    bad = [r.date for r in series
           if not (S.is_daytime(r.sunrise, *coords) is True
                   and S.is_daytime(r.sunrise - sec, *coords) is False
                   and S.is_daytime(r.sunset, *coords) is False
                   and S.is_daytime(r.sunset - sec, *coords) is True)]
    print(f"  {label:<8} {len(series) - len(bad)}/{len(series)} суток")
    check(not bad, f"гарантия округления {label}", f"нарушено в {bad[:5]}")


# ---------------------------------------------------------------------------
# 4. is_daytime - собственно поставляемая функция
# ---------------------------------------------------------------------------
hdr("4. is_daytime() - таблица истинности вокруг событий")
minute = timedelta(minutes=1)
print(f"{'дата':<12} {'-1м до вос':>11} {'+1м после':>10} {'полдень':>8} "
      f"{'-1м до зак':>11} {'+1м после':>10} {'солн.полночь':>13}")
for iso in ("2026-03-20", "2026-06-21", "2026-12-21"):
    st = S.sun_times(date.fromisoformat(iso), *MOSCOW, tz)
    solar_midnight = st.solar_noon + timedelta(hours=12)
    probes = [
        (st.sunrise - minute, False),
        (st.sunrise + minute, True),
        (st.solar_noon, True),
        (st.sunset - minute, True),
        (st.sunset + minute, False),
        (solar_midnight, False),
    ]
    got = [S.is_daytime(m, *MOSCOW) for m, _ in probes]
    print(f"{iso:<12} " + " ".join(f"{str(g):>10}" if i else f"{str(g):>11}"
                                   for i, g in enumerate(got)))
    for (m, want), g in zip(probes, got):
        check(g == want, f"is_daytime {iso} @ {m.strftime('%H:%M')}", f"ждали {want}, получили {g}")

# граница ровно в момент события (секундная гарантия округления)
st = S.sun_times(date(2026, 7, 31), *MOSCOW, tz)
for label, moment in (("за 1 сек до восхода", st.sunrise - timedelta(seconds=1)),
                      ("РОВНО восход", st.sunrise),
                      ("за 1 сек до заката", st.sunset - timedelta(seconds=1)),
                      ("РОВНО закат", st.sunset)):
    print(f"\n{label:<22} {moment.strftime('%H:%M:%S')}: "
          f"is_daytime={S.is_daytime(moment, *MOSCOW)!s:<5} "
          f"высота {S.solar_altitude(moment, *MOSCOW):+.4f} (порог {S.HORIZON_ALTITUDE})")


# ---------------------------------------------------------------------------
# 5. Переход на летнее время
# ---------------------------------------------------------------------------
hdr("5. ПЕРЕХОД НА ЛЕТНЕЕ ВРЕМЯ (в MSK его нет, берём Australia/Sydney)")
syd = ZoneInfo("Australia/Sydney")
for iso, what in (("2026-10-03", "накануне"),
                  ("2026-10-04", "ПЕРЕВОД ВПЕРЁД: 02:00 -> 03:00 AEDT"),
                  ("2026-10-05", "после"),
                  ("2026-04-04", "накануне"),
                  ("2026-04-05", "ПЕРЕВОД НАЗАД: 03:00 -> 02:00 AEST (час повторяется)"),
                  ("2026-04-06", "после")):
    st = S.sun_times(date.fromisoformat(iso), *SYDNEY, syd)
    off = st.sunrise.utcoffset()
    print(f"{iso} {what:<48} восход {st.sunrise.strftime('%H:%M:%S')} "
          f"закат {st.sunset.strftime('%H:%M:%S')} UTC{off} {st.sunrise.tzname()}")
    check(st.sunrise < st.sunset, f"DST {iso} порядок", "")

# скачок стенных часов на переводе - ровно час, и он не мешает is_daytime
a = S.sun_times(date(2026, 10, 3), *SYDNEY, syd)
b = S.sun_times(date(2026, 10, 4), *SYDNEY, syd)
wall = (b.sunrise.hour * 60 + b.sunrise.minute) - (a.sunrise.hour * 60 + a.sunrise.minute)
real = elapsed_min(a.sunrise, b.sunrise) - 1440
print(f"\nвосход по СТЕННЫМ часам сдвинулся на {wall:+d} мин (ожидаем ~+60: перевели стрелки)")
print(f"восход по РЕАЛЬНОМУ времени сдвинулся на {real:+.2f} мин (ожидаем единицы минут)")
check(50 < wall < 70, "стенной скачок ~час", str(wall))
check(abs(real) < 10, "реальный сдвиг мал", f"{real:.2f}")

# day_length в сутки перевода стрелок. Наивное `sunset - sunrise` при ОДНОМ И
# ТОМ ЖЕ объекте tzinfo даёт стенную разницу без поправки на перевод; свойство
# day_length обязано считать через UTC.
naive_diff = b.sunset - b.sunrise
print(f"\n04.10 (перевод): day_length={b.day_length}, "
      f"наивное sunset-sunrise={naive_diff} (совпадают: перевод был ночью, вне светового дня)")
apr = S.sun_times(date(2026, 4, 5), *SYDNEY, syd)
print(f"05.04 (перевод): day_length={apr.day_length}, "
      f"вчера {S.sun_times(date(2026, 4, 4), *SYDNEY, syd).day_length}, "
      f"завтра {S.sun_times(date(2026, 4, 6), *SYDNEY, syd).day_length}")
check(timedelta(hours=11) < apr.day_length < timedelta(hours=12), "day_length в сутки перевода",
      str(apr.day_length))
# синтетическая проверка самого свойства: перевод стрелок ВНУТРИ светового дня
# (Чили переводит в 00:00 локального, поэтому берём Lord Howe - 30-минутный сдвиг)
lhi = ZoneInfo("Australia/Lord_Howe")
lhi_st = S.sun_times(date(2026, 10, 4), -31.5553, 159.0821, lhi)
print(f"Lord Howe 04.10 (сдвиг +30 мин): восход {lhi_st.sunrise.strftime('%H:%M:%S')} "
      f"закат {lhi_st.sunset.strftime('%H:%M:%S')} day_length={lhi_st.day_length}")
check(timedelta(hours=12) < lhi_st.day_length < timedelta(hours=13), "Lord Howe day_length",
      str(lhi_st.day_length))
# 02:30 04.10.2026 в Сиднее НЕ СУЩЕСТВУЕТ - модуль не должен падать
try:
    ghost = datetime(2026, 10, 4, 2, 30, tzinfo=syd)
    print(f"несуществующее локальное 02:30 04.10: is_daytime={S.is_daytime(ghost, *SYDNEY)} "
          f"(исключения нет)")
    check(True, "несуществующее время", "")
except Exception as exc:
    check(False, "несуществующее время", repr(exc))
# неоднозначный час 05.04.2026 02:30 - оба fold
try:
    for fold in (0, 1):
        amb = datetime(2026, 4, 5, 2, 30, tzinfo=syd, fold=fold)
        print(f"неоднозначное 02:30 05.04 fold={fold}: UTC{amb.utcoffset()} "
              f"альт={S.solar_altitude(amb, *SYDNEY):+.3f}")
    check(True, "неоднозначное время", "")
except Exception as exc:
    check(False, "неоднозначное время", repr(exc))


# ---------------------------------------------------------------------------
# 6. Наивные datetime, пояс системы
# ---------------------------------------------------------------------------
hdr("6. НАИВНЫЕ datetime И ПОЯС СИСТЕМЫ")
print(f"local_timezone()              : {S.local_timezone()!r}")
print(f"/etc/timezone                 : {S._local_timezone_name()!r}")
naive = datetime(2026, 7, 31, 12, 0)
aware = naive.replace(tzinfo=S.local_timezone())
an, aa = S.solar_altitude(naive, *MOSCOW), S.solar_altitude(aware, *MOSCOW)
print(f"наивный 2026-07-31 12:00      : высота {an:+.4f}")
print(f"тот же момент, осведомлённый  : высота {aa:+.4f}")
print(f"политика: наивный трактуется как ЛОКАЛЬНОЕ время -> совпадение "
      f"{abs(an - aa) < 1e-9}")
check(abs(an - aa) < 1e-9, "наивный == локальный", f"{an} vs {aa}")
check(S.is_daytime(naive, *MOSCOW) is True, "is_daytime от наивного", "")
check(S.is_daytime() in (True, False), "is_daytime() без аргументов", "")


# ---------------------------------------------------------------------------
# 7. next_transition
# ---------------------------------------------------------------------------
hdr("7. next_transition()")
st = S.sun_times(date(2026, 7, 31), *MOSCOW, tz)
for probe_label, probe in (("после восхода (день)", st.sunrise + timedelta(hours=1)),
                           ("после заката (ночь)", st.sunset + timedelta(hours=1))):
    nxt = S.next_transition(probe, *MOSCOW, tz)
    before, at = S.is_daytime(probe, *MOSCOW), S.is_daytime(nxt, *MOSCOW)
    print(f"{probe_label:<22} {probe.strftime('%d.%m %H:%M:%S')} -> смена "
          f"{nxt.strftime('%d.%m %H:%M:%S')}   is_daytime: {before} -> {at} "
          f"(за 1 сек до: {S.is_daytime(nxt - timedelta(seconds=1), *MOSCOW)})")
    check(nxt > probe, f"next_transition вперёд ({probe_label})", "")
    # ГАРАНТИЯ: в возвращённый момент состояние УЖЕ сменилось (иначе таймер
    # фазы 2 сработает, а тема не переключится)
    check(at != before, f"next_transition: состояние уже сменилось ({probe_label})", "")
    check(S.is_daytime(nxt - timedelta(seconds=1), *MOSCOW) == before,
          f"next_transition: за секунду до ещё старое ({probe_label})", "")
# должен совпасть с закатом тех же суток
nxt = S.next_transition(st.sunrise + timedelta(hours=1), *MOSCOW, tz)
print(f"сверка: закат этих суток {st.sunset.strftime('%H:%M:%S')}, "
      f"next_transition {nxt.strftime('%H:%M:%S')}, "
      f"расхождение {abs((nxt - st.sunset).total_seconds()):.1f} сек")
check(abs((nxt - st.sunset).total_seconds()) <= 2, "next_transition == закат", "")
# полярная ночь: смены нет
polar = datetime(2026, 12, 21, 12, 0, tzinfo=ZoneInfo("Europe/Oslo"))
nxt = S.next_transition(polar, *LONGYEARBYEN, ZoneInfo("Europe/Oslo"), max_days=30)
print(f"Лонгйир 21.12 + 30 суток      : {nxt} (полярная ночь -> ожидаем None)")
check(nxt is None, "полярная ночь: нет смены", str(nxt))
nxt = S.next_transition(polar, *LONGYEARBYEN, ZoneInfo("Europe/Oslo"), max_days=190)
print(f"Лонгйир 21.12 + 190 суток     : {nxt} (Солнце возвращается ~16 февраля)")
check(nxt is not None and nxt.month == 2, "полярная ночь кончается в феврале", str(nxt))


# ---------------------------------------------------------------------------
# 8. load_location
# ---------------------------------------------------------------------------
hdr("8. load_location() - чтение координат из KlipperScreen.conf")
tmp = tempfile.mkdtemp()


def write_conf(name, body):
    path = os.path.join(tmp, name)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(body)
    return path


cases = [
    ("валидные", "[main]\ntheme: big-font-light\nlatitude: 59.9375\nlongitude: 30.3086\n",
     (59.9375, 30.3086)),
    ("отрицательные (юг/запад)", "[main]\nlatitude: -33.8688\nlongitude: -70.6693\n",
     (-33.8688, -70.6693)),
    ("опций нет", "[main]\ntheme: big-font\n", (S.DEFAULT_LATITUDE, S.DEFAULT_LONGITUDE)),
    ("мусор вместо числа", "[main]\nlatitude: где-то тут\nlongitude: 30.0\n",
     (S.DEFAULT_LATITUDE, S.DEFAULT_LONGITUDE)),
    ("вне диапазона", "[main]\nlatitude: 555\nlongitude: 30.0\n",
     (S.DEFAULT_LATITUDE, S.DEFAULT_LONGITUDE)),
    ("реальный конфиг принтера как есть",
     "[main]\nkeyboard_navigation: true\nfont_size: medium\nshow_cursor: false\n"
     "theme: big-font-light\n\n#~# --- Do not edit below this line. ---- #~#\n#~#\n#~# \n",
     (S.DEFAULT_LATITUDE, S.DEFAULT_LONGITUDE)),
]
for i, (label, body, expect) in enumerate(cases):
    got = S.load_location(write_conf(f"c{i}.conf", body))
    print(f"{label:<36} -> {got}   (ждали {expect})")
    check(got == expect, f"load_location {label}", f"{got} != {expect}")
got = S.load_location(os.path.join(tmp, "нет-такого.conf"))
print(f"{'файла нет':<36} -> {got}   (ждали Москву)")
check(got == (S.DEFAULT_LATITUDE, S.DEFAULT_LONGITUDE), "load_location нет файла", str(got))


# ---------------------------------------------------------------------------
# 9. Производительность на самом Orange Pi
# ---------------------------------------------------------------------------
hdr("9. ПРОИЗВОДИТЕЛЬНОСТЬ (на этом же Orange Pi H3)")
import time as _t
now = datetime.now(tz)
t0 = _t.perf_counter()
for _ in range(1000):
    S.is_daytime(now, *MOSCOW)
t1 = _t.perf_counter()
print(f"is_daytime()      : {(t1 - t0) * 1000:.3f} мс на 1000 вызовов "
      f"= {(t1 - t0) * 1000:.3f} мкс каждый")
t0 = _t.perf_counter()
for _ in range(100):
    S.sun_times(date(2026, 7, 31), *MOSCOW, tz)
t1 = _t.perf_counter()
print(f"sun_times()       : {(t1 - t0) * 10:.3f} мс на вызов (бисекция ~16 итераций x2)")
t0 = _t.perf_counter()
S.next_transition(now, *MOSCOW, tz)
t1 = _t.perf_counter()
print(f"next_transition() : {(t1 - t0) * 1000:.2f} мс (обычный случай, смена в пределах суток)")
t0 = _t.perf_counter()
S.next_transition(polar, *LONGYEARBYEN, ZoneInfo("Europe/Oslo"), max_days=190)
t1 = _t.perf_counter()
print(f"next_transition() : {(t1 - t0) * 1000:.2f} мс (худший случай: полярная ночь, 190 суток)")


# ---------------------------------------------------------------------------
hdr("ИТОГ")
print(f"проверок выполнено: {CHECKS}")
if FAILURES:
    print(f"ПРОВАЛЕНО: {len(FAILURES)}")
    for f in FAILURES:
        print("  ! " + f)
    sys.exit(1)
print("ВСЕ ПРОВЕРКИ ПРОЙДЕНЫ")
