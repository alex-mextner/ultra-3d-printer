#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Оффлайновый расчёт восхода/заката для автопереключения светлой/тёмной темы
KlipperScreen.

Только stdlib (math + datetime + zoneinfo + configparser) - ни сети, ни pip,
ни API-ключей. Принтер - встроенный Orange Pi, у которого интернета может не
быть вообще; кроме того, локальный расчёт детерминирован и потому проверяем
тестами, чего про поход в чужой API сказать нельзя.

Алгоритм - стандартный NOAA (solar position / equation of time), тот же, что в
NOAA Solar Calculator. Точность для наших широт - порядка минуты (см. блок
проверки в docstring-е внизу и отчёт по фазе).

Ключевое архитектурное решение
------------------------------
Есть ровно ОДНА первичная функция - `solar_altitude()` (геометрическая высота
Солнца над горизонтом в градусах). Всё остальное выводится из неё:

* `is_daytime()` - это буквально `solar_altitude(...) > HORIZON_ALTITUDE`;
* `sun_times()` находит восход/закат бисекцией той же самой `solar_altitude`
  по порогу `HORIZON_ALTITUDE`.

Так сделано намеренно, вместо классической закрытой формулы через `acos`
часового угла. Причины:

1. Ответы согласованы ПО ПОСТРОЕНИЮ. Если считать `is_daytime` по высоте, а
   восход - через `acos`, два способа разъезжаются на десятки секунд, и в
   логе появляется абсурд вида "сейчас ночь, закат был 40 секунд назад".
2. Полярный день/ночь получаются бесплатно: на такой день пересечения порога
   просто не находится, и не нужно ловить выход `acos` за область определения.
3. Нет проблемы "событие уехало в соседние сутки". Восход/закат для локальной
   даты D могут по стенным часам попасть в D-1 или D+1 (высокие широты + край
   часового пояса), и любая логика вида "sunrise <= now <= sunset по дате D"
   на этом ломается. Проверка по высоте Солнца от даты не зависит вообще.

Полярный день / полярная ночь
-----------------------------
Не считаются ошибкой и не роняют вызывающий код:

* полярный день  -> `sunrise=None`, `sunset=None`, `polar_day=True`,
  `is_daytime()` возвращает True весь день (тема остаётся светлой);
* полярная ночь  -> `sunrise=None`, `sunset=None`, `polar_night=True`,
  `is_daytime()` возвращает False весь день (тема остаётся тёмной);
* переходные сутки (Солнце взошло, но в эти сутки уже не сядет, или наоборот)
  отдают ровно одно из полей заполненным, второе - None. Это честнее, чем
  подставлять полночь.

Часовые пояса и переход на летнее время
---------------------------------------
Внутри всё считается в UTC, конвертация в локальное время - ровно один раз, на
краях. Пояс НЕ захардкожен: `local_timezone()` определяет реальный настроенный
пояс системы (TZ -> /etc/timezone -> симлинк /etc/localtime) и отдаёт
`ZoneInfo`, который умеет переходы на летнее время. Сутки привязываются к
локальному ПОЛУДНЮ, а не к полуночи: полуночи 00:00 в некоторых поясах в дни
перевода стрелок физически не существует, а 12:00 существует всегда.

Координаты
----------
По умолчанию Москва (55.7558 N, 37.6173 E). ВНИМАНИЕ: это предположение,
выведенное из часового пояса устройства (Europe/Moscow), с пользователем оно НЕ
подтверждалось. Реальные координаты задаются в `[main]` файла
KlipperScreen.conf опциями `latitude` / `longitude` (см. `load_location()`).
"""

import configparser
import logging
import math
import os
from datetime import date as _date
from datetime import datetime, time, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - на устройстве Python 3.13, ветка недостижима
    ZoneInfo = None

__all__ = [
    "HORIZON_ALTITUDE",
    "DEFAULT_LATITUDE",
    "DEFAULT_LONGITUDE",
    "SunTimes",
    "solar_altitude",
    "sun_times",
    "is_daytime",
    "next_transition",
    "load_location",
    "local_timezone",
]

# Геометрическая высота центра Солнца, при которой видимый верхний край касается
# горизонта: -50 угловых минут = рефракция атмосферы (34') + видимый радиус
# диска Солнца (16'). Это стандартное определение восхода/заката (NOAA, USNO).
HORIZON_ALTITUDE = -0.833

# Москва. ПРЕДПОЛОЖЕНИЕ по часовому поясу устройства, НЕ подтверждено пользователем.
DEFAULT_LATITUDE = 55.7558
DEFAULT_LONGITUDE = 37.6173

# Где искать KlipperScreen.conf на принтере (первый существующий побеждает).
CONFIG_SEARCH_PATHS = (
    "~/printer_data/config/KlipperScreen.conf",
    "/home/ultra/printer_data/config/KlipperScreen.conf",
    "~/KlipperScreen/KlipperScreen.conf",
)

# JD полуночи unix-эпохи: 1970-01-01T00:00Z == JD 2440587.5
_JD_UNIX_EPOCH = 2440587.5
# JD эпохи J2000.0 (2000-01-01T12:00 TT), от неё считаются юлианские столетия.
_JD_J2000 = 2451545.0

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# Часовой пояс
# --------------------------------------------------------------------------

def _local_timezone_name():
    """Имя пояса из конфигурации системы ('Europe/Moscow') либо None."""
    env = os.environ.get("TZ")
    if env and "/" in env:
        return env
    try:
        with open("/etc/timezone", encoding="utf-8") as fh:
            name = fh.read().strip()
        if name:
            return name
    except OSError:
        pass
    try:
        # /etc/localtime обычно симлинк на /usr/share/zoneinfo/<Area>/<City>
        resolved = os.path.realpath("/etc/localtime")
        marker = "/zoneinfo/"
        if marker in resolved:
            return resolved.split(marker, 1)[1]
    except OSError:
        pass
    return None


def local_timezone():
    """Реальный настроенный пояс системы как tzinfo.

    Пояс НЕ захардкожен: сначала пробуем получить именованный `ZoneInfo` (он
    один умеет переходы на летнее время для произвольной даты). Если имя
    определить не удалось - откатываемся на текущее смещение системы; это
    fixed-offset пояс, для дат по другую сторону перевода стрелок он даст
    сдвиг на час, поэтому используется только как крайний fallback.
    """
    name = _local_timezone_name()
    if name and ZoneInfo is not None:
        try:
            return ZoneInfo(name)
        except Exception as exc:  # неизвестное имя / нет tzdata
            logger.warning("daynight_sun: не удалось загрузить пояс %r (%s)", name, exc)
    tz = datetime.now().astimezone().tzinfo
    logger.warning("daynight_sun: именованный пояс не определён, fallback на %s", tz)
    return tz


# --------------------------------------------------------------------------
# Солнечная позиция (NOAA)
# --------------------------------------------------------------------------

def _as_utc(moment, tz=None):
    """datetime -> осведомлённый datetime в UTC.

    Наивный datetime трактуется как локальное время (`tz`, по умолчанию пояс
    системы) - именно это удобно вызывающему коду, который передаёт
    `datetime.now()`. В неоднозначный час осеннего перевода стрелок берётся
    первое (доперёводное) вхождение - политика `fold=0` из stdlib.
    """
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=tz or local_timezone())
    return moment.astimezone(timezone.utc)


def _julian_day(moment_utc):
    """Юлианская дата для осведомлённого datetime."""
    return moment_utc.timestamp() / 86400.0 + _JD_UNIX_EPOCH


def _solar_declination_and_eot(julian_day):
    """(склонение Солнца в градусах, уравнение времени в минутах).

    Формулы NOAA General Solar Position Calculations - те же, что в
    официальной таблице NOAA_Solar_Calculations_day.xls.
    """
    t = (julian_day - _JD_J2000) / 36525.0  # юлианские столетия от J2000.0

    # Средняя геометрическая долгота Солнца, град
    mean_long = (280.46646 + t * (36000.76983 + t * 0.0003032)) % 360.0
    # Средняя аномалия Солнца, град
    mean_anom = 357.52911 + t * (35999.05029 - 0.0001537 * t)
    # Эксцентриситет орбиты Земли
    eccent = 0.016708634 - t * (0.000042037 + 0.0000001267 * t)

    m_rad = math.radians(mean_anom)
    # Уравнение центра
    center = (
        math.sin(m_rad) * (1.914602 - t * (0.004817 + 0.000014 * t))
        + math.sin(2 * m_rad) * (0.019993 - 0.000101 * t)
        + math.sin(3 * m_rad) * 0.000289
    )
    true_long = mean_long + center

    omega = 125.04 - 1934.136 * t  # долгота восходящего узла орбиты Луны
    # Видимая долгота (поправка на нутацию и аберрацию)
    app_long = true_long - 0.00569 - 0.00478 * math.sin(math.radians(omega))

    # Средний наклон эклиптики + поправка
    mean_obliq = 23.0 + (26.0 + (21.448 - t * (46.815 + t * (0.00059 - t * 0.001813))) / 60.0) / 60.0
    obliq = mean_obliq + 0.00256 * math.cos(math.radians(omega))

    declination = math.degrees(
        math.asin(math.sin(math.radians(obliq)) * math.sin(math.radians(app_long)))
    )

    # Уравнение времени, минуты
    y = math.tan(math.radians(obliq / 2.0)) ** 2
    l_rad = math.radians(mean_long)
    eot = 4.0 * math.degrees(
        y * math.sin(2 * l_rad)
        - 2.0 * eccent * math.sin(m_rad)
        + 4.0 * eccent * y * math.sin(m_rad) * math.cos(2 * l_rad)
        - 0.5 * y * y * math.sin(4 * l_rad)
        - 1.25 * eccent * eccent * math.sin(2 * m_rad)
    )
    return declination, eot


def _minutes_of_utc_day(moment_utc):
    return (
        moment_utc.hour * 60.0
        + moment_utc.minute
        + moment_utc.second / 60.0
        + moment_utc.microsecond / 60_000_000.0
    )


def solar_altitude(moment, latitude=DEFAULT_LATITUDE, longitude=DEFAULT_LONGITUDE, tz=None):
    """Геометрическая высота центра Солнца над горизонтом, градусы.

    Единственная первичная функция модуля - всё остальное выведено из неё.
    Высота ГЕОМЕТРИЧЕСКАЯ (без рефракции); рефракция и радиус диска учтены в
    пороге `HORIZON_ALTITUDE`, с которым её сравнивают.

    `moment` - datetime (наивный трактуется как локальное время).
    """
    moment_utc = _as_utc(moment, tz)
    declination, eot = _solar_declination_and_eot(_julian_day(moment_utc))

    # Истинное солнечное время в минутах: гринвичское время суток + уравнение
    # времени + поправка на долготу (4 минуты на градус).
    true_solar_minutes = (_minutes_of_utc_day(moment_utc) + eot + 4.0 * longitude) % 1440.0
    hour_angle = true_solar_minutes / 4.0 - 180.0  # градусы, 0 в солнечный полдень

    lat_rad = math.radians(latitude)
    dec_rad = math.radians(declination)
    cos_zenith = math.sin(lat_rad) * math.sin(dec_rad) + math.cos(lat_rad) * math.cos(
        dec_rad
    ) * math.cos(math.radians(hour_angle))
    # Клампим: накопленная ошибка округления может дать |cos| чуть больше 1
    cos_zenith = max(-1.0, min(1.0, cos_zenith))
    return 90.0 - math.degrees(math.acos(cos_zenith))


# --------------------------------------------------------------------------
# Восход / закат
# --------------------------------------------------------------------------

class SunTimes:
    """Результат расчёта суток. Все datetime осведомлённые, в локальном поясе."""

    __slots__ = ("date", "sunrise", "sunset", "solar_noon", "max_altitude", "min_altitude")

    def __init__(self, date, sunrise, sunset, solar_noon, max_altitude, min_altitude):
        self.date = date
        self.sunrise = sunrise
        self.sunset = sunset
        self.solar_noon = solar_noon
        self.max_altitude = max_altitude
        self.min_altitude = min_altitude

    @property
    def polar_day(self):
        """Солнце не заходит: светло круглые сутки."""
        return self.min_altitude > HORIZON_ALTITUDE

    @property
    def polar_night(self):
        """Солнце не восходит: темно круглые сутки."""
        return self.max_altitude <= HORIZON_ALTITUDE

    @property
    def day_length(self):
        """timedelta между восходом и закатом; None если событий нет."""
        if self.sunrise is None or self.sunset is None:
            return None
        # ВЫЧИТАЕМ ЧЕРЕЗ UTC, И ЭТО НЕ ПРИДИРКА. Если у двух осведомлённых
        # datetime ОДИН И ТОТ ЖЕ объект tzinfo, Python вычитает СТЕННОЕ время
        # без поправки на переход (docs/datetime: "the common tzinfo attribute
        # is ignored ... no time zone adjustment is done"). В сутки перевода
        # стрелок длина светового дня уехала бы ровно на час. Поймано тестом
        # verify_sun.py на Australia/Sydney 2026-10-04.
        return self.sunset.astimezone(timezone.utc) - self.sunrise.astimezone(timezone.utc)

    def describe(self):
        """Однострочник для лога."""
        if self.polar_day:
            return f"{self.date}: полярный день (Солнце не заходит)"
        if self.polar_night:
            return f"{self.date}: полярная ночь (Солнце не восходит)"
        rise = self.sunrise.strftime("%H:%M:%S") if self.sunrise else "нет"
        setting = self.sunset.strftime("%H:%M:%S") if self.sunset else "нет"
        length = self.day_length
        tail = f", световой день {length}" if length else ""
        return f"{self.date}: восход {rise}, закат {setting}{tail}"

    def __repr__(self):
        return f"<SunTimes {self.describe()}>"


def _solar_noon_utc(anchor_utc, longitude, latitude):
    """Момент верхней кульминации Солнца рядом с `anchor_utc` (в UTC).

    Итеративно: смотрим, насколько истинное солнечное время в текущей точке
    отличается от полудня, и сдвигаем точку на эту разницу. Сходится за 2-3
    шага, потому что уравнение времени за несколько часов почти не меняется.
    """
    moment = anchor_utc
    for _ in range(4):
        _, eot = _solar_declination_and_eot(_julian_day(moment))
        true_solar_minutes = (_minutes_of_utc_day(moment) + eot + 4.0 * longitude) % 1440.0
        offset = true_solar_minutes - 720.0
        # Приводим к (-720, 720]: сдвигаемся к БЛИЖАЙШЕМУ полудню, а не к
        # вчерашнему/завтрашнему.
        if offset > 720.0:
            offset -= 1440.0
        elif offset <= -720.0:
            offset += 1440.0
        if abs(offset) < 1.0 / 60.0:  # < 1 секунды - достаточно
            break
        moment -= timedelta(minutes=offset)
    return moment


def _bisect_horizon_crossing(low, high, latitude, longitude, rising, precision_s=0.5):
    """Момент пересечения `HORIZON_ALTITUDE` внутри [low, high] (оба в UTC).

    Вызывается только когда пересечение гарантированно есть. На интервале от
    солнечной полуночи до полудня высота монотонно растёт, от полудня до
    следующей полуночи - монотонно падает, так что бисекция корректна.
    """
    while (high - low).total_seconds() > precision_s:
        mid = low + (high - low) / 2
        above = solar_altitude(mid, latitude, longitude) > HORIZON_ALTITUDE
        if above == rising:
            high = mid
        else:
            low = mid
    return low + (high - low) / 2


def _round_to_second(moment):
    if moment.microsecond >= 500_000:
        moment += timedelta(seconds=1)
    return moment.replace(microsecond=0)


def _snap_to_second(crossing_utc, latitude, longitude, state_after):
    """Округлить момент пересечения до целой секунды ПО НАПРАВЛЕНИЮ перехода.

    Возвращает первую целую секунду, в которую новое состояние уже наступило,
    то есть гарантируется
    `is_daytime(результат) == state_after`.

    Зачем не обычное округление к ближайшему: вызывающий код ставит таймер на
    возвращённый момент и в этот момент спрашивает `is_daytime()`. Если
    округлить к ближайшему, можно попасть на долю секунды РАНЬШЕ настоящего
    восхода - таймер сработает, `is_daytime()` вернёт ещё старое значение, и
    тема не переключится. Проверка после округления стоит одного вычисления
    высоты и снимает этот класс ошибок совсем.

    Считается в UTC (сюда приходит UTC), поэтому арифметика с timedelta точна.

    Реализация - явный перебор секунд в окне +-2 с вокруг найденного момента, а
    НЕ округление вверх. Округления вверх недостаточно: бисекция знает момент
    пересечения с точностью +-0.25 с, и если она ошиблась в позднюю сторону
    через границу целой секунды, ceil даёт секунду ПОЗЖЕ первой подходящей.
    Ровно это и случилось на 24 из 365 суток (проверка "гарантия округления" в
    verify_sun.py ловила 2026-01-06 и компанию). Окно +-2 с гарантированно
    накрывает пересечение: base >= C-1.25 с, значит base-2 < C < base+2, а
    высота Солнца в такой окрестности строго монотонна, поэтому первое
    совпадение при проходе слева направо - искомая первая секунда.
    """
    base = crossing_utc.replace(microsecond=0)
    for offset in range(-2, 3):
        moment = base + timedelta(seconds=offset)
        if (solar_altitude(moment, latitude, longitude) > HORIZON_ALTITUDE) == state_after:
            return moment
    return base + timedelta(seconds=1)  # недостижимо, страховка от зацикливания


def sun_times(day=None, latitude=DEFAULT_LATITUDE, longitude=DEFAULT_LONGITUDE, tz=None):
    """Восход, закат и полдень для локальных суток `day`.

    `day` - date, datetime или None (сегодня). Если передан datetime, берётся
    его локальная календарная дата.

    Возвращает `SunTimes` с осведомлёнными datetime в локальном поясе.
    Полярный день/ночь - `sunrise`/`sunset` равны None, см. флаги
    `polar_day`/`polar_night`, исключения не бросаются.

    Гарантия округления (см. `_snap_to_second`): `sunrise` - первая целая
    секунда, в которую `is_daytime()` уже True, `sunset` - первая целая
    секунда, в которую `is_daytime()` уже False. То есть
    `is_daytime(st.sunrise) == True` и `is_daytime(st.sunset) == False` всегда.
    """
    tz = tz or local_timezone()

    if day is None:
        day = datetime.now(tz).date()
    elif isinstance(day, datetime):
        day = (day.astimezone(tz) if day.tzinfo else day.replace(tzinfo=tz)).date()
    elif not isinstance(day, _date):
        raise TypeError(f"sun_times: ожидался date/datetime/None, получен {type(day)!r}")

    # Привязываемся к локальному ПОЛУДНЮ, а не к полуночи: 00:00 в дни перевода
    # стрелок в некоторых поясах не существует вовсе, а 12:00 есть всегда.
    anchor_utc = _as_utc(datetime.combine(day, time(12, 0)), tz)

    noon_utc = _solar_noon_utc(anchor_utc, longitude, latitude)
    prev_midnight = noon_utc - timedelta(hours=12)  # солнечная полночь до полудня
    next_midnight = noon_utc + timedelta(hours=12)  # солнечная полночь после

    alt_noon = solar_altitude(noon_utc, latitude, longitude)
    alt_prev = solar_altitude(prev_midnight, latitude, longitude)
    alt_next = solar_altitude(next_midnight, latitude, longitude)

    sunrise = sunset = None
    # Каждую половину суток проверяем отдельно: в переходные полярные сутки
    # может быть восход без заката или закат без восхода.
    if alt_prev <= HORIZON_ALTITUDE < alt_noon:
        sunrise = _bisect_horizon_crossing(prev_midnight, noon_utc, latitude, longitude, True)
        sunrise = _snap_to_second(sunrise, latitude, longitude, True)
    if alt_next <= HORIZON_ALTITUDE < alt_noon:
        sunset = _bisect_horizon_crossing(noon_utc, next_midnight, latitude, longitude, False)
        sunset = _snap_to_second(sunset, latitude, longitude, False)

    return SunTimes(
        date=day,
        sunrise=sunrise.astimezone(tz) if sunrise else None,
        sunset=sunset.astimezone(tz) if sunset else None,
        solar_noon=_round_to_second(noon_utc.astimezone(tz)),
        max_altitude=alt_noon,
        min_altitude=min(alt_prev, alt_next),
    )


def is_daytime(now=None, latitude=DEFAULT_LATITUDE, longitude=DEFAULT_LONGITUDE, tz=None):
    """True если Солнце сейчас над горизонтом (пора светлой темы).

    Считается напрямую по высоте Солнца, а не сравнением с расписанием
    восход/закат: так корректно работают и полярный день/ночь, и случай, когда
    событие по стенным часам уехало в соседние сутки.

    `now` - datetime (наивный трактуется как локальное время) или None (сейчас).
    """
    if now is None:
        now = datetime.now(tz or local_timezone())
    return solar_altitude(now, latitude, longitude, tz) > HORIZON_ALTITUDE


def next_transition(
    now=None,
    latitude=DEFAULT_LATITUDE,
    longitude=DEFAULT_LONGITUDE,
    tz=None,
    max_days=190,
    step_minutes=30,
):
    """Ближайший момент после `now`, когда `is_daytime()` сменит значение.

    Нужен, чтобы фаза переключения тем ставила один таймер на точное время, а
    не опрашивала солнце каждую минуту.

    Возвращает осведомлённый datetime в локальном поясе либо None, если за
    `max_days` смены не нашлось (реальный случай - полярный день/ночь: они
    длятся месяцами, но не больше полугода, отсюда дефолт 190 суток).

    Гарантируется, что в возвращённый момент новое состояние УЖЕ наступило:
    `is_daytime(next_transition(t)) != is_daytime(t)`. Это важно, потому что
    вызывающий код ставит на этот момент таймер и сразу спрашивает
    `is_daytime()` - см. `_snap_to_second`.

    Оговорка про честность: поиск грубый, шагом `step_minutes`, потом уточнение
    бисекцией. Если световой день (или ночь) короче шага - такое бывает
    буквально в одни-двое суток в году на самом краю полярного круга - пара
    переходов может быть пропущена. Для 55.8 N это недостижимо: там минимальная
    ночь около 5 часов. Шаг 30 минут выбран сознательно: 10-минутный на 190
    сутках это ~27k вычислений (заметная пауза на H3), 30-минутный - ~9k.
    """
    tz = tz or local_timezone()
    if now is None:
        now = datetime.now(tz)
    start_utc = _as_utc(now, tz)

    current = solar_altitude(start_utc, latitude, longitude) > HORIZON_ALTITUDE
    step = timedelta(minutes=step_minutes)
    limit = start_utc + timedelta(days=max_days)

    low = start_utc
    probe = start_utc + step
    while probe <= limit:
        state = solar_altitude(probe, latitude, longitude) > HORIZON_ALTITUDE
        if state != current:
            crossing = _bisect_horizon_crossing(low, probe, latitude, longitude, state)
            return _snap_to_second(crossing, latitude, longitude, state).astimezone(tz)
        low = probe
        probe = probe + step
    return None


# --------------------------------------------------------------------------
# Координаты из конфига
# --------------------------------------------------------------------------

def _valid_coords(latitude, longitude):
    return -90.0 <= latitude <= 90.0 and -180.0 <= longitude <= 180.0


def load_location(config_path=None):
    """(latitude, longitude) из секции [main] файла KlipperScreen.conf.

    Опции `latitude` / `longitude` в градусах, десятичной дробью, север и
    восток положительные:

        [main]
        latitude: 55.7558
        longitude: 37.6173

    Любая проблема (нет файла, нет опций, не число, значение вне диапазона) -
    молча-с-предупреждением-в-лог откат на Москву. Ронять из-за этого
    KlipperScreen нельзя.

    ВАЖНО для фазы установки: KlipperScreen валидирует конфиг и на незнакомую
    опцию в [main] показывает модалку "Invalid config file" ВМЕСТО интерфейса
    (screen.py: `if self._config.errors: show_error_modal(...); return` до
    `base_panel.activate()`). Поэтому latitude/longitude обязаны быть добавлены
    в кортеж `numbers` секции "main" в `ks_includes/config.py` - иначе киоск не
    поднимется. Отдельный патч, см. README.
    """
    candidates = [config_path] if config_path else list(CONFIG_SEARCH_PATHS)
    for candidate in candidates:
        if not candidate:
            continue
        path = os.path.expanduser(candidate)
        if not os.path.isfile(path):
            continue
        parser = configparser.ConfigParser(strict=False)
        try:
            parser.read(path, encoding="utf-8")
        except (configparser.Error, OSError, UnicodeDecodeError) as exc:
            logger.warning("daynight_sun: не разобрать %s (%s)", path, exc)
            continue
        if not parser.has_section("main"):
            continue
        raw_lat = parser.get("main", "latitude", fallback=None)
        raw_lon = parser.get("main", "longitude", fallback=None)
        if raw_lat is None or raw_lon is None:
            continue
        try:
            latitude = float(raw_lat)
            longitude = float(raw_lon)
        except ValueError:
            logger.warning(
                "daynight_sun: latitude/longitude в %s не числа (%r/%r), беру Москву",
                path, raw_lat, raw_lon,
            )
            return DEFAULT_LATITUDE, DEFAULT_LONGITUDE
        if not _valid_coords(latitude, longitude):
            logger.warning(
                "daynight_sun: latitude/longitude вне диапазона (%s/%s), беру Москву",
                latitude, longitude,
            )
            return DEFAULT_LATITUDE, DEFAULT_LONGITUDE
        return latitude, longitude
    return DEFAULT_LATITUDE, DEFAULT_LONGITUDE


# --------------------------------------------------------------------------
# CLI - для ручной проверки прямо на принтере
# --------------------------------------------------------------------------

def _main():
    import argparse

    ap = argparse.ArgumentParser(description="Восход/закат оффлайн (NOAA)")
    ap.add_argument("--lat", type=float, default=None, help="широта, градусы")
    ap.add_argument("--lon", type=float, default=None, help="долгота, градусы")
    ap.add_argument("--date", default=None, help="дата YYYY-MM-DD (по умолчанию сегодня)")
    ap.add_argument("--config", default=None, help="путь к KlipperScreen.conf")
    args = ap.parse_args()

    if args.lat is None or args.lon is None:
        latitude, longitude = load_location(args.config)
        source = "конфиг/дефолт"
    else:
        latitude, longitude = args.lat, args.lon
        source = "аргументы"

    tz = local_timezone()
    day = _date.fromisoformat(args.date) if args.date else datetime.now(tz).date()
    times = sun_times(day, latitude, longitude, tz)
    now = datetime.now(tz)

    print(f"координаты : {latitude}, {longitude} ({source})")
    print(f"часовой пояс: {tz}")
    print(f"сейчас     : {now.isoformat(timespec='seconds')}")
    print(times.describe())
    print(f"полдень    : {times.solar_noon.strftime('%H:%M:%S')} "
          f"(высота {times.max_altitude:.2f}, минимум {times.min_altitude:.2f})")
    # Ниже - про ТЕКУЩИЙ момент, а не про --date (важно не перепутать при отладке)
    print(f"высота сейчас : {solar_altitude(now, latitude, longitude):+.2f} "
          f"(порог {HORIZON_ALTITUDE})")
    print(f"сейчас день   : {is_daytime(now, latitude, longitude)}")
    nxt = next_transition(now, latitude, longitude, tz)
    print(f"смена сейчас->: {nxt.isoformat(timespec='seconds') if nxt else 'не в ближайшие 190 суток'}")


if __name__ == "__main__":
    _main()
