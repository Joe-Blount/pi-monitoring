"""The node file, the validator, the sun and the decision, joined up.

Every other control test exercises one module against hand-built inputs. That
is how a crash reached the deployed path: the decision's tests build ladders
by hand with timedelta offsets, the validator hands out floats of seconds,
and adding a float to a datetime raises. Both modules were right by their own
convention and the seam between them had nobody checking it.

So this drives the real node file through the real validator into the real
decision, with real computed sun times. No fixtures, no hand-built ladders.
Anything that disagrees about a unit across those boundaries fails here.
"""

import datetime
import pathlib
import zoneinfo

import pytest

from monitoring import config, drivers
from monitoring.control import decide, spec, sun

REPO = pathlib.Path(__file__).resolve().parent.parent
NODE = REPO / "sites" / "blind1" / "downstairs.yaml"

#: The site, rounded to the tenth of a degree it is configured at.
LATITUDE = 37.0
LONGITUDE = -97.4
ZONE = zoneinfo.ZoneInfo("America/Chicago")

#: Late June, where the solar windows run closest to the clock tier's edges.
DAY = datetime.date(2026, 6, 21)

#: Comfortably past the minimum run and off times in the node file, so these
#: assertions are about the ladder rather than about a timer holding a load.
PAST_THE_TIMERS = 100_000.0


@pytest.fixture(scope="module")
def loads():
    """The shipped control block, as the controller would parse it.

    Devices are forced enabled because the machine this file describes does
    not exist yet and its battery is still polled by the node upstairs. The
    refusal that causes is asserted in tests/test_node_files.py; here the
    question is whether the parsed block works, not whether it may run.
    """
    node = config.load(NODE, drivers.names())
    for device in node.devices:
        device.enabled = True
    return spec.load_control(node)


@pytest.fixture(scope="module")
def suns():
    days = []
    for offset in (-1, 0, 1):
        date = DAY + datetime.timedelta(days=offset)
        rise, _, set_ = sun.events(date, LATITUDE, LONGITUDE, ZONE)
        days.append({"sunrise": rise, "sunset": set_})
    return days


def reading(soc, cell=3.30, pack=13.4, age=1.0):
    return {"bms": {"fields": {"state_of_charge_pct": soc,
                               "cell_min_volts": cell}, "age_seconds": age},
            "mppt": {"fields": {"battery_volts": pack}, "age_seconds": age}}


def settled(loads, suns, soc, when, **kwargs):
    """Drive the decision until the ladder has stopped moving.

    A tier needs consecutive readings to latch and the load starts connected,
    so the minimum run time holds the first shed. Both are walked past here
    so the assertion that follows is about the schedule.
    """
    readings = reading(soc, **kwargs)
    switches = {"shedding_enabled": True, "loads": {}}
    state = {}
    decision = None
    for step in range(4):
        decisions, state = decide.decide(
            when, PAST_THE_TIMERS + step * PAST_THE_TIMERS, True, readings,
            switches, loads, state, suns)
        decision = decisions["cameras"]
    return decision


def at(hour, minute=0):
    return datetime.datetime(DAY.year, DAY.month, DAY.day, hour, minute,
                             tzinfo=ZONE)


# -- the seam that was broken ----------------------------------------------

def test_a_parsed_solar_schedule_can_be_evaluated(loads, suns):
    """The crash this file exists for.

    The validator converts a window offset and the decision adds it to a
    datetime. Those have to agree about the unit, and when they did not, the
    loop raised the first time the window tier latched with a trusted clock,
    which is when the battery is low and the schedule matters most.
    """
    assert settled(loads, suns, 24.0, at(19, 30)) is not None


def test_every_duration_in_the_parsed_block_is_the_unit_its_reader_expects(
        loads):
    """Each of these crosses the same boundary the solar offset did. A
    staleness limit compared against an age in seconds has to be a number; an
    offset added to a datetime has to be a timedelta. Asserted by type, since
    both are perfectly valid values of the wrong kind.
    """
    ladder = loads["cameras"]["shed_ladder"]
    for name in ("stale_after", "restore_dwell"):
        assert isinstance(ladder[name], float), name
    for name in ("min_run", "min_off"):
        assert isinstance(loads["cameras"][name], float), name
    for tier in ladder["tiers"]:
        schedule = tier["schedule"]
        if schedule["type"] == "clock":
            for edge in ("on_from", "on_to"):
                assert isinstance(schedule[edge], int), edge
        if schedule["type"] == "solar":
            for window in schedule["on_windows"]:
                for edge in ("from", "to"):
                    event, offset = window[edge]
                    assert isinstance(event, str)
                    assert isinstance(offset, datetime.timedelta), (
                        "%s offset is %s, which cannot be added to a "
                        "datetime" % (edge, type(offset).__name__))


# -- the ladder the owner asked for, end to end ----------------------------

@pytest.mark.parametrize("soc,hour,minute,connected", [
    # Above every threshold: on, whatever the hour.
    (60.0, 3, 0, True),
    (60.0, 12, 0, True),
    (60.0, 23, 0, True),

    # Below fifty: the night goes.
    (45.0, 12, 0, True),
    (45.0, 21, 59, True),
    (45.0, 22, 1, False),
    (45.0, 4, 59, False),
    (45.0, 5, 1, True),

    # Below twenty-five: only the movement windows, which on this date run
    # 05:10 to 08:10 and 18:53 to 21:53.
    (24.0, 5, 0, False),
    (24.0, 6, 30, True),
    (24.0, 8, 30, False),
    (24.0, 12, 0, False),
    (24.0, 19, 30, True),
    (24.0, 21, 59, False),

    # Below ten: never.
    (9.0, 6, 30, False),
    (9.0, 19, 30, False),
    (9.0, 12, 0, False),
])
def test_the_shipped_ladder_behaves_as_agreed(loads, suns, soc, hour, minute,
                                              connected):
    """The thresholds, the windows and the sun arithmetic together.

    These are the answers the owner described, checked through the file he
    will edit rather than through a fixture that agrees with the code.
    """
    decision = settled(loads, suns, soc, at(hour, minute))
    assert decision.connected is connected, decision.reason


def test_the_cell_floor_overrides_the_schedule_end_to_end(loads, suns):
    """Parsed bounds, parsed sources, inside a window that would otherwise
    permit the load."""
    decision = settled(loads, suns, 60.0, at(19, 30), cell=3.10)
    assert not decision.connected
    assert decision.reason_code == decide.FLOOR
    assert "cell_min_volts" in decision.reason


def test_the_pack_voltage_can_shed_on_its_own_end_to_end(loads, suns):
    """The second floor source, with the bounds the node file gives it."""
    decision = settled(loads, suns, 60.0, at(12), pack=12.1)
    assert not decision.connected
    assert decision.reason_code == decide.FLOOR
    assert "battery_volts" in decision.reason


def test_an_untrusted_clock_leaves_the_cameras_running(loads, suns):
    """Which is also their de-energized state, so nothing happens at the
    relay. Checked here because the suspension depends on the parsed
    schedule types rather than on hand-written ones."""
    readings = reading(45.0)
    switches = {"shedding_enabled": True, "loads": {}}
    state = {}
    for step in range(4):
        decisions, state = decide.decide(
            at(23), PAST_THE_TIMERS * (step + 1), False, readings, switches,
            loads, state, [])
        decision = decisions["cameras"]
    assert decision.connected
    assert decision.reason_code == decide.SCHEDULE_SUSPENDED
