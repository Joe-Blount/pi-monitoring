"""The node file, the validator, the sun and the decision, joined up.

Every other control test exercises one module against hand-built inputs, and
a hand-built input is chosen to already agree with the module it feeds: the
decision's tests build ladders by hand with timedelta offsets, the
validator's tests confirm it hands out floats of seconds. Each module is
right by its own convention, so a disagreement about units at the seam
between them -- a float added to a datetime, say -- is invisible to both
modules' own tests and shows up only where the seam itself is exercised.

So this drives the real node file through the real validator into the real
decision, with real computed sun times. No fixtures, no hand-built ladders:
anything that disagrees about a unit across those boundaries fails here.
"""

import datetime
import pathlib
import zoneinfo

import pytest

import json

from monitoring import config, drivers, runner
from monitoring.control import clock, decide, files, loop, relay, spec, sun
from monitoring.drivers import control as control_driver

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


# -- the unit seam between the validator and the decision -------------------

def test_a_parsed_solar_schedule_can_be_evaluated(loads, suns):
    """The seam this file exists to guard.

    The validator converts a window offset and the decision adds it to a
    datetime. Those have to agree about the unit: a disagreement would only
    surface the first time the window tier latches with a trusted clock,
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


# -- the seam between the loop and the collector that reports it ------------
#
# The loop writes a document and a driver reads it. They are deliberately
# not coupled by an import: a driver here may import the driver base and
# nothing else, so that any driver can be lifted into its own package by
# moving one file. The document's shape is therefore an agreement between two
# modules that never see each other, which is the same kind of agreement the
# unit seam above is, and it fails the same way -- silently, on the one node
# where it matters, with everything looking collected.

BOOT = "a-boot-identity-the-test-controls"


def run_the_real_loop(tmp_path, soc, cell=3.30):
    """The shipping loop, run until the ladder has settled, writing the
    document it really writes.

    Everything the loop needs from the world is injected except the writer:
    files.write_json is left in place, because the point here is the bytes on
    disk, and a recording publisher would assert the shape of a dict this
    test had just handed over.

    Several passes, because a tier needs distinct readings to latch and the
    load starts connected, so the minimum run time holds the first shed. Each
    pass carries its own reading stamp, since the same sample seen twice is
    one reading.
    """
    node = config.load(NODE, drivers.names())
    for device in node.devices:
        device.enabled = True
    published = str(tmp_path / "control.json")
    clock_state = {"mono": 0.0}
    machine = loop.Loop(
        node, {"latitude": LATITUDE, "longitude": LONGITUDE}, ZONE,
        relays={"cameras": relay.Pretend(23, relay.ENERGIZE_TO_SHED)},
        now=lambda: at(19, 30),
        monotonic=lambda: clock_state["mono"],
        # Injected, because the suite runs on machines with no real-time
        # clock and no synchronisation marker, where the real check correctly
        # refuses to place a schedule. What is under test here is the
        # schedule being placed and reported.
        trust=lambda high_water=None: clock.Trust(True, "under test"),
        readings=lambda: {
            "bms": {"fields": {"state_of_charge_pct": soc,
                               "cell_min_volts": cell},
                    "age_seconds": 1.0, "taken_at": clock_state["mono"]},
            "mppt": {"fields": {"battery_volts": 13.4}, "age_seconds": 1.0}},
        switches=lambda: {"shedding_enabled": True, "loads": {}},
        state_path=str(tmp_path / "state.json"),
        published_path=published,
        high_water_path=str(tmp_path / "hw.json"),
        boot=BOOT)
    for step in range(4):
        clock_state["mono"] = step * PAST_THE_TIMERS
        machine.once()
    return published


def reader(path, boot=BOOT, mono=0.0):
    device = control_driver.ControlDriver("control", {"path": path},
                                          {"location": "control"})
    device._boot_id = lambda: boot
    device._monotonic = lambda: mono
    return device


def by_load(points):
    """The reader's points keyed by load, with the loop's own under None."""
    return {one.tags.get("load"): one.fields for one in points}


def test_the_collector_reads_the_document_the_loop_writes(tmp_path):
    """The whole reporting path, with nothing hand-built in the middle.

    A controller nobody can see is the failure worth guarding: the shed
    level and the reason code are the only evidence that load shedding is
    doing anything, and the one thing most worth knowing is that the
    controller has stopped, because every load is then at its fail-safe and
    nothing is managing the battery.
    """
    points = by_load(reader(run_the_real_loop(tmp_path, 24.0),
                            mono=3 * PAST_THE_TIMERS).read())
    cameras, loop_point = points["cameras"], points[None]

    # Powered and shed at the same time, which is the state a reader most
    # needs the two fields to be able to express: the battery has put the
    # cameras on the movement-window schedule, and 19:30 is inside the
    # evening window, so they are running on a schedule rather than freely.
    assert cameras["powered"] == 1.0
    assert cameras["shed_level"] == 2.0
    assert cameras["reason_code"] == float(decide.STATE_OF_CHARGE)
    assert isinstance(cameras["reason"], str) and cameras["reason"]
    assert cameras["schedule_suspended"] == 0.0
    assert loop_point["clock_synced"] == 1.0
    assert loop_point["published_age_seconds"] >= 0.0


def test_the_reported_shed_level_and_reason_follow_the_battery(tmp_path):
    """Two passes that differ only in the battery, so the fields cannot be
    constants that happen to read correctly."""
    (tmp_path / "low").mkdir()
    (tmp_path / "high").mkdir()
    low = by_load(reader(run_the_real_loop(tmp_path / "low", 9.0),
                         mono=3 * PAST_THE_TIMERS).read())["cameras"]
    high = by_load(reader(run_the_real_loop(tmp_path / "high", 60.0),
                          mono=3 * PAST_THE_TIMERS).read())["cameras"]

    assert low["powered"] == 0.0
    assert low["shed_level"] == 3.0
    assert low["reason_code"] == float(decide.STATE_OF_CHARGE)

    assert high["powered"] == 1.0
    assert high["shed_level"] == 0.0


def test_each_load_is_its_own_point_tagged_with_the_load(tmp_path):
    """So that one chart repeats over loads and a table over all of them is
    generic: a load added to the node file needs no panel edited."""
    points = reader(run_the_real_loop(tmp_path, 24.0),
                    mono=3 * PAST_THE_TIMERS).read()
    assert [one.tags for one in points] == [{}, {"load": "cameras"}]


def test_the_floor_owner_names_the_shipped_source_that_tripped_it(tmp_path):
    """Through the real node file: the cell minimum is the first floor source
    declared there, and the owner is published as device.field."""
    cameras = by_load(reader(run_the_real_loop(tmp_path, 60.0, cell=3.10),
                             mono=3 * PAST_THE_TIMERS).read())["cameras"]
    assert cameras["reason_code"] == float(decide.FLOOR)
    assert cameras["floor_latched"] == 1.0
    assert cameras["floor_latched_by"] == "bms.cell_min_volts"


def test_the_age_is_on_the_loop_point_and_measured_by_the_reader(tmp_path):
    """One age for the document, not one per load, and computed against the
    reader's own clock rather than copied out of the file, so a wedged loop
    cannot look current."""
    points = by_load(reader(run_the_real_loop(tmp_path, 24.0),
                            mono=3 * PAST_THE_TIMERS + 42.0).read())
    assert points[None]["published_age_seconds"] == 42.0
    assert "published_age_seconds" not in points["cameras"]


#: A document written by hand, so that the rendered lines below can be
#: pinned as literals rather than derived from the loop.
DOCUMENT = {
    "boot_id": BOOT, "monotonic": 4.0,
    "fields": {"clock_synced": 1.0, "clock_reason": "synchronised",
               "passes": 7.0, "loop_monotonic": 4.0},
    "loads": {"cameras": {
        "powered": 1.0, "level": 0.0, "shed_level": 2.0, "reason_code": 1.0,
        "reason": "state_of_charge_pct=24.0 below 50%",
        "schedule_suspended": 0.0, "decided_on": 24.0,
        "floor_latched": 0.0, "level_changes": 3.0}}}


def written(tmp_path, document):
    path = tmp_path / "control.json"
    path.write_text(json.dumps(document))
    return str(path)


def test_the_lines_the_collector_writes_for_the_controller(tmp_path,
                                                            monkeypatch):
    """The wire format, end to end through the runner: the load tag beside
    the device's own tags, the field names bare, and the age only on the
    point that has no load."""
    path = written(tmp_path, DOCUMENT)
    node_file = tmp_path / "node.yaml"
    node_file.write_text(
        "site: s\nnode: n\nmeasurement: m\ndevices:\n"
        "  - name: control\n    driver: control\n    mode: poll\n"
        "    params: {path: %s}\n    tags: {location: control}\n" % path)
    boot_file = tmp_path / "boot_id"
    boot_file.write_text(BOOT + "\n")
    monkeypatch.setattr(control_driver, "BOOT_ID_PATH", str(boot_file))
    monkeypatch.setattr(control_driver, "_monotonic", lambda: 10.0)

    node = config.load(node_file, drivers.names())
    result = runner.read_device(node, node.find("control"), 1700000000)
    assert result.ok, result.error
    assert result.lines == [
        'm,device=control,location=control clock_reason="synchronised",'
        'clock_synced=1.0,loop_monotonic=4.0,ok=1.0,passes=7.0,'
        'published_age_seconds=6.0 1700000000',
        'm,device=control,load=cameras,location=control decided_on=24.0,'
        'floor_latched=0.0,level=0.0,level_changes=3.0,ok=1.0,powered=1.0,'
        'reason="state_of_charge_pct=24.0 below 50%",reason_code=1.0,'
        'schedule_suspended=0.0,shed_level=2.0 1700000000']


def test_a_document_that_names_no_loads_is_a_fault(tmp_path):
    """A load the controller drives and nobody can see is the failure this
    device exists to prevent, so a document with no place for loads is
    refused rather than read as a controller with none."""
    document = dict(DOCUMENT)
    del document["loads"]
    with pytest.raises(Exception) as raised:
        reader(written(tmp_path, document)).read()
    assert "loads" in str(raised.value)


def test_a_document_with_no_loads_points_at_a_controller_not_restarted(
        tmp_path):
    """A controller still running code from before an install writes that
    shape until the service restarts, which is the usual cause, and the
    message has to send the reader to it rather than to the controller's
    logic."""
    document = dict(DOCUMENT)
    del document["loads"]
    with pytest.raises(Exception) as raised:
        reader(written(tmp_path, document)).read()
    assert "restart monitoring-control" in str(raised.value)


def test_a_load_published_with_no_usable_value_is_a_fault(tmp_path):
    """A point carrying nothing but ok=1 would say the load reported
    healthily while saying nothing about it."""
    document = dict(DOCUMENT, loads={"cameras": {"powered": None,
                                                 "floor_latched": True}})
    with pytest.raises(Exception) as raised:
        reader(written(tmp_path, document)).read()
    assert "cameras" in str(raised.value)


def test_a_load_published_with_no_fields_is_a_fault(tmp_path):
    document = dict(DOCUMENT, loads={"cameras": {}})
    with pytest.raises(Exception) as raised:
        reader(written(tmp_path, document)).read()
    assert "cameras" in str(raised.value)


def test_a_controller_with_every_load_disabled_publishes_only_itself(
        tmp_path):
    """Not a fault: the node file said so, and the loop's own fields still
    say it is alive."""
    points = reader(written(tmp_path, dict(DOCUMENT, loads={}))).read()
    assert [one.tags for one in points] == [{}]


def test_a_document_from_a_previous_boot_is_reported_as_a_fault(tmp_path):
    """Its age is measured on a clock that restarts at zero, so an old
    document cannot be aged. A controller that has not published since the
    machine started must read as broken rather than as current, or a loop
    that never came up would publish its predecessor's decisions for ever.
    """
    path = run_the_real_loop(tmp_path, 24.0)
    with pytest.raises(Exception) as raised:
        reader(path, boot="a-later-boot").read()
    assert "not written during this boot" in str(raised.value)


def test_a_missing_document_names_what_it_means(tmp_path):
    """Read by somebody looking at a dashboard with a gap in it. A silent
    nothing would read as a collector problem rather than as a stopped
    controller."""
    with pytest.raises(Exception) as raised:
        reader(str(tmp_path / "never-written.json")).read()
    message = str(raised.value)
    assert "fail-safe" in message and "check the service" in message


def test_the_collectors_check_names_the_loads_it_found(tmp_path):
    """What somebody sees from `collect --check` while standing at the
    machine, which is the only place this string is read."""
    assert "cameras" in reader(run_the_real_loop(tmp_path, 24.0)).check()


def test_the_document_the_loop_writes_lands_where_the_node_files_point(
        tmp_path):
    """The default path, not the injected one. Every node file names this
    literal, and a test above holds them to it; this is the other half of
    that agreement."""
    assert files.PUBLISHED == "/run/monitoring/control.json"
