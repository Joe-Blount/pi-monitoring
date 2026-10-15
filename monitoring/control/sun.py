"""Sunrise and sunset from a date and a place, computed locally.

The schedule that matters most here is solar rather than clock-based: the
cameras run through the hours deer move, which track the sun and not the
wall. So this has to be right, and it has to be right without a network,
because the machine asking is off-grid and its uplink is the first thing to
go.

The arithmetic is the NOAA solar position series. It is about forty lines and
needs only the date, so there is nothing to cache and nothing to fetch.

Two definitions worth stating, because both are places to be quietly wrong.

Sunrise is the moment the upper limb of the disc clears the horizon with
atmospheric refraction applied, which is a zenith angle of 90.833 degrees
rather than 90. That is why the day is a little longer than twelve hours at
an equinox, and an implementation tuned to produce exactly twelve hours there
is about four minutes out for the rest of the year.

Results come back in the caller's timezone, built by converting from UTC, so
a daylight saving transition is handled by the zone database rather than by
arithmetic here. On the day the clocks move, sunrise and sunset both fall
after the transition at this latitude, so both land in the new offset.
"""

import datetime
import math

#: Zenith angle of the sun at sunrise and sunset, in degrees. Greater than 90
#: because the disc has a radius and the atmosphere bends light over the
#: horizon: the sun is geometrically below it at the moment it appears.
ZENITH_DEGREES = 90.833

#: Julian day number of the J2000.0 epoch, which the series is expressed from.
J2000 = 2451545.0

#: Days in a Julian century.
DAYS_PER_CENTURY = 36525.0

MINUTES_PER_DAY = 1440.0


class NoSunEvent(ValueError):
    """The sun neither rises nor sets on this date at this latitude.

    Cannot happen at the latitudes this project runs at, and raised rather
    than papered over because a polar caller deserves an error instead of a
    number that looks like a time.
    """


#: How many times each event is re-solved. The series is evaluated at the
#: moment of the event, but the moment is what is being looked for, so it
#: starts from midday and converges.
#:
#: One pass is not enough. Sunset here is thirteen hours from midday, and
#: declination moves about a quarter of a degree in that time near an
#: equinox, which is where the error lands: a single pass is a minute out on
#: the September equinox and seconds out in midwinter. Two passes bring the
#: whole year inside the almanac's own rounding.
SOLVE_PASSES = 2


def _julian_day_midnight(date):
    """The Julian day number at 00:00 UTC on `date`."""
    year, month, day = date.year, date.month, date.day
    if month <= 2:
        year -= 1
        month += 12
    a = year // 100
    b = 2 - a + a // 4
    return (math.floor(365.25 * (year + 4716))
            + math.floor(30.6001 * (month + 1))
            + day + b - 1524.5)


def _solar_terms(julian_century):
    """Declination and the equation of time, in degrees and minutes."""
    t = julian_century

    mean_longitude = (280.46646 + t * (36000.76983 + t * 0.0003032)) % 360.0
    mean_anomaly = 357.52911 + t * (35999.05029 - 0.0001537 * t)
    eccentricity = 0.016708634 - t * (0.000042037 + 0.0000001267 * t)

    anomaly = math.radians(mean_anomaly)
    centre = (math.sin(anomaly) * (1.914602 - t * (0.004817 + 0.000014 * t))
              + math.sin(2 * anomaly) * (0.019993 - 0.000101 * t)
              + math.sin(3 * anomaly) * 0.000289)

    true_longitude = mean_longitude + centre
    apparent_longitude = (true_longitude
                          - 0.00569
                          - 0.00478 * math.sin(math.radians(
                              125.04 - 1934.136 * t)))

    mean_obliquity = (23.0 + (26.0 + ((21.448 - t * (46.815 + t * (
        0.00059 - t * 0.001813)))) / 60.0) / 60.0)
    obliquity = mean_obliquity + 0.00256 * math.cos(
        math.radians(125.04 - 1934.136 * t))

    declination = math.degrees(math.asin(
        math.sin(math.radians(obliquity))
        * math.sin(math.radians(apparent_longitude))))

    # The equation of time: how far ahead or behind the real sun runs against
    # a clock. It is why the earliest sunrise of the year is not the solstice.
    y = math.tan(math.radians(obliquity / 2.0)) ** 2
    equation_minutes = 4.0 * math.degrees(
        y * math.sin(2 * math.radians(mean_longitude))
        - 2 * eccentricity * math.sin(anomaly)
        + 4 * eccentricity * y * math.sin(anomaly)
        * math.cos(2 * math.radians(mean_longitude))
        - 0.5 * y * y * math.sin(4 * math.radians(mean_longitude))
        - 1.25 * eccentricity * eccentricity * math.sin(2 * anomaly))

    return declination, equation_minutes


def _hour_angle_degrees(latitude, declination):
    """Half the length of the day, as an angle.

    Raises when the sun does not cross the horizon, which is the only way the
    cosine can leave its domain.
    """
    lat = math.radians(latitude)
    dec = math.radians(declination)
    cosine = (math.cos(math.radians(ZENITH_DEGREES))
              / (math.cos(lat) * math.cos(dec))
              - math.tan(lat) * math.tan(dec))
    if not -1.0 <= cosine <= 1.0:
        raise NoSunEvent(
            "at latitude %.3f the sun does not cross the horizon on this "
            "date" % latitude)
    return math.degrees(math.acos(cosine))


def _solve(julian_midnight, latitude, longitude, direction):
    """Minutes after 00:00 UTC of one event.

    `direction` is -1 for sunrise, 0 for solar noon and +1 for sunset.

    The series wants the moment of the event, which is the thing being looked
    for, so it starts from midday and re-solves. Four minutes per degree
    throughout, because the earth turns 360 degrees in 1440 minutes.
    """
    minutes = 720.0
    for _ in range(SOLVE_PASSES):
        century = ((julian_midnight + minutes / MINUTES_PER_DAY - J2000)
                   / DAYS_PER_CENTURY)
        declination, equation_minutes = _solar_terms(century)
        noon = 720.0 - 4.0 * longitude - equation_minutes
        if direction == 0:
            minutes = noon
        else:
            minutes = noon + direction * 4.0 * _hour_angle_degrees(
                latitude, declination)
    return minutes


def events(date, latitude, longitude, zone):
    """Sunrise, solar noon and sunset on `date` at a place, in `zone`.

    `date` is a local calendar date and `zone` its timezone. `longitude` is
    positive east, so this hemisphere is negative.

    Returns three timezone-aware datetimes in `zone`.
    """
    julian_midnight = _julian_day_midnight(date)

    # Built from midnight UTC on the same calendar date and then converted, so
    # a value past 1440 rolls into the next UTC day and still lands on the
    # intended local date. West of Greenwich, sunset routinely does.
    base = datetime.datetime(date.year, date.month, date.day,
                             tzinfo=datetime.timezone.utc)
    return tuple(
        (base + datetime.timedelta(
            minutes=_solve(julian_midnight, latitude, longitude, direction))
         ).astimezone(zone)
        for direction in (-1, 0, 1))


def sunrise(date, latitude, longitude, zone):
    return events(date, latitude, longitude, zone)[0]


def sunset(date, latitude, longitude, zone):
    return events(date, latitude, longitude, zone)[2]
