"""Sunrise and sunset against an independent authority.

Every expected value here comes from the US Naval Observatory's one-day
service for the configured location. Fetched rather than computed, so these
tests compare the implementation against somebody else's arithmetic instead
of against itself, which is the only way a solar routine can be checked
without waiting a year and looking out of the window.
"""

import datetime
import zoneinfo

import pytest

from monitoring.control import sun

LAT = 37.0
LON = -97.4
ZONE = zoneinfo.ZoneInfo("America/Chicago")

#: Local clock times from the US Naval Observatory, apiversion 4.0.1, for
#: 37.0 N 97.4 W.
#:
#: On a daylight saving transition day the offset is the one in force after
#: the switch, because sunrise and sunset both fall after it at this
#: latitude. Taking the naive answer for the first of November gives 07:54,
#: which is a central daylight label for a sunrise the local clock shows at
#: 06:54 central standard. That is an hour of error that looks authoritative.
ALMANAC = [
    ((2026, 1, 15), "07:42", "17:37"),
    ((2026, 3, 8), "07:51", "19:31"),      # clocks spring forward at 02:00
    ((2026, 3, 20), "07:33", "19:42"),     # equinox
    ((2026, 6, 13), "06:09", "20:50"),     # earliest sunrise of the year
    ((2026, 6, 21), "06:10", "20:53"),     # solstice
    ((2026, 6, 27), "06:12", "20:53"),
    ((2026, 7, 1), "06:14", "20:53"),      # latest sunset of the year
    ((2026, 7, 15), "06:22", "20:49"),
    ((2026, 9, 22), "07:18", "19:26"),     # equinox
    ((2026, 11, 1), "06:54", "17:32"),     # clocks fall back at 02:00
    ((2026, 12, 21), "07:39", "17:16"),    # solstice
]

#: The almanac publishes whole minutes, so a correct implementation can differ
#: from it by up to half a minute by rounding alone, and the worst case here is
#: 28 seconds. Forty-five is loose enough for that rounding and tight enough to
#: fail the mistake it was written for: evaluating the solar series once at
#: midday instead of re-solving at the moment of the event, which is a minute
#: out at an equinox because declination moves fastest there.
TOLERANCE = datetime.timedelta(seconds=45)


def _expected(day, text):
    hour, minute = (int(part) for part in text.split(":"))
    return datetime.datetime(day[0], day[1], day[2], hour, minute,
                             tzinfo=ZONE)


@pytest.mark.parametrize("day,rise,set_", ALMANAC)
def test_sunrise_and_sunset_match_the_almanac(day, rise, set_):
    got_rise, _, got_set = sun.events(datetime.date(*day), LAT, LON, ZONE)
    assert abs(got_rise - _expected(day, rise)) <= TOLERANCE, (
        "sunrise %s, almanac %s" % (got_rise, rise))
    assert abs(got_set - _expected(day, set_)) <= TOLERANCE, (
        "sunset %s, almanac %s" % (got_set, set_))


@pytest.mark.parametrize("day,rise,set_", ALMANAC)
def test_every_event_lands_on_the_date_it_was_asked_for(day, rise, set_):
    """West of Greenwich, sunset is on the next UTC day.

    Building the result from midnight UTC and converting is what makes that
    come out on the intended local date. An implementation that clamped to
    the UTC date would put every sunset a day early.
    """
    wanted = datetime.date(*day)
    for event in sun.events(wanted, LAT, LON, ZONE):
        assert event.date() == wanted


@pytest.mark.parametrize("day,rise,set_", ALMANAC)
def test_noon_sits_midway_between_sunrise_and_sunset(day, rise, set_):
    """The day is symmetric about solar noon, which the equation of time
    moves but does not tilt."""
    got_rise, got_noon, got_set = sun.events(
        datetime.date(*day), LAT, LON, ZONE)
    midpoint = got_rise + (got_set - got_rise) / 2
    assert abs(midpoint - got_noon) <= datetime.timedelta(seconds=30)


def test_an_equinox_day_is_longer_than_twelve_hours():
    """Not exactly twelve, and the difference is the point.

    Sunrise is the upper limb clearing the horizon with refraction applied,
    so the sun is up for a few minutes longer than geometry alone allows. An
    implementation tuned to produce exactly twelve hours here is about four
    minutes out all year.
    """
    for day in ((2026, 3, 20), (2026, 9, 22)):
        rise, _, set_ = sun.events(datetime.date(*day), LAT, LON, ZONE)
        length = set_ - rise
        assert datetime.timedelta(hours=12, minutes=4) < length, (
            "%s came out at %s, which is geometry without refraction" % (day, length))
        assert length < datetime.timedelta(hours=12, minutes=15)


def test_the_longest_and_shortest_days_are_the_solstices():
    def length(day):
        rise, _, set_ = sun.events(datetime.date(*day), LAT, LON, ZONE)
        return set_ - rise

    summer = length((2026, 6, 21))
    winter = length((2026, 12, 21))
    assert summer > length((2026, 3, 20)) > winter
    assert summer > length((2026, 9, 22)) > winter
    assert summer > datetime.timedelta(hours=14)
    assert winter < datetime.timedelta(hours=10)


def test_the_earliest_sunrise_is_not_the_solstice():
    """The equation of time moves the extremes off the solstice.

    This is why a window checked only on the twenty-first of June misses the
    tightest margin of the year: the dawn window opens earlier in mid-June
    than it does on the longest day.
    """
    def rise_time(day):
        rise, _, _ = sun.events(datetime.date(*day), LAT, LON, ZONE)
        return rise.time()

    assert rise_time((2026, 6, 13)) < rise_time((2026, 6, 21))

    def set_time(day):
        _, _, set_ = sun.events(datetime.date(*day), LAT, LON, ZONE)
        return set_.time()

    assert set_time((2026, 7, 1)) >= set_time((2026, 6, 21))


@pytest.mark.parametrize("day,offset_hours", [
    ((2026, 1, 15), -6),      # central standard
    ((2026, 7, 15), -5),      # central daylight
    ((2026, 3, 8), -5),       # the day the clocks go forward
    ((2026, 11, 1), -6),      # the day they go back
])
def test_results_carry_the_offset_in_force_after_any_transition(day,
                                                                offset_hours):
    """Both events fall after the 02:00 switch at this latitude, so both are
    in the new offset. Arithmetic on a fixed offset would be an hour out on
    two days a year."""
    rise, _, set_ = sun.events(datetime.date(*day), LAT, LON, ZONE)
    expected = datetime.timedelta(hours=offset_hours)
    assert rise.utcoffset() == expected
    assert set_.utcoffset() == expected


def test_a_latitude_with_no_sunrise_is_refused_rather_than_guessed():
    """Impossible where this runs, and an error rather than a number that
    looks like a time."""
    with pytest.raises(sun.NoSunEvent):
        sun.events(datetime.date(2026, 6, 21), 85.0, LON, ZONE)
    with pytest.raises(sun.NoSunEvent):
        sun.events(datetime.date(2026, 12, 21), 85.0, LON, ZONE)


def test_longitude_is_positive_east():
    """A sign error here moves every event by thirteen hours at this
    longitude, which is the kind of mistake that looks like a timezone bug
    and gets fixed in the wrong place."""
    west, _, _ = sun.events(datetime.date(2026, 6, 21), LAT, -97.4, ZONE)
    east, _, _ = sun.events(datetime.date(2026, 6, 21), LAT, 97.4, ZONE)
    assert west > east
