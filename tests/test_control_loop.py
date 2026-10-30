"""The control loop, with every ambient thing held still.

The loop is the only module that reads a clock, touches a file or drives a
pin, so these tests inject all three. Nothing here waits and nothing owns
hardware.
"""

import datetime
import zoneinfo

import pytest

from monitoring.control import clock, files, loop, relay
from monitoring.control.spec import SpecError

ZONE = zoneinfo.ZoneInfo("America/Chicago")
LOCATION = {"latitude": 37.0, "longitude": -97.4}

#: A boot identity the tests control. The real one comes from /proc, which
#: does not exist on every machine the suite runs on, and the loop refuses to
#: start without one.
BOOT = "test-boot-identity"


class FakeDevice:
    def __init__(self, name, enabled=True):
        self.name = name
        self.enabled = enabled


class FakeNode:
    def __init__(self, loads, devices=("bms", "mppt")):
        self.devices = [FakeDevice(name) for name in devices]
        self.control = {"loads": loads}


LADDER = {
    "source": {"device": "bms", "field": "state_of_charge_pct"},
    "stale_after": "10m",
    "release_margin_pct": 15,
    "enter_after_readings": 1,
    "restore_dwell": 0,
    "floor": {"sources": [
        {"device": "bms", "field": "cell_min_volts",
         "shed_below": 3.15, "restore_above": 3.25}]},
    "tiers": [
        {"below_pct": 50, "schedule": {"type": "clock",
                                       "on_from": "05:00", "on_to": "22:00"}},
        {"below_pct": 10, "schedule": {"type": "never"}},
    ],
}

CAMERAS = {"type": "dc_branch", "enabled": True, "control_pin": 23,
           "polarity": "energize_to_shed", "shed_ladder": LADDER}


def reading(soc=60.0, cell=3.30, age=1.0):
    return {"bms": {"fields": {"state_of_charge_pct": soc,
                               "cell_min_volts": cell},
                    "age_seconds": age}}


def build(tmp_path, readings=None, at=None, mono=0.0, trusted=True,
          switches=None, loads=None, boot=BOOT):
    """A loop whose world is entirely under the test's control."""
    pin = relay.Pretend(23, relay.ENERGIZE_TO_SHED)
    published = {}

    state = {"now": at or datetime.datetime(2026, 6, 21, 12, tzinfo=ZONE),
             "mono": mono, "readings": readings if readings is not None
             else reading()}

    machine = loop.Loop(
        FakeNode(loads or {"cameras": CAMERAS}), LOCATION, ZONE,
        relays={"cameras": pin},
        now=lambda: state["now"],
        monotonic=lambda: state["mono"],
        trust=lambda high_water=None: clock.Trust(trusted, "under test"),
        readings=lambda: state["readings"],
        switches=lambda: switches or {"shedding_enabled": True, "loads": {}},
        state_path=str(tmp_path / "state.json"),
        published_path=str(tmp_path / "published.json"),
        high_water_path=str(tmp_path / "hw.json"),
        publish=lambda _path, data: published.update(data),
        boot=boot)
    return machine, pin, published, state


# -- not acting before there is anything to act on --------------------------

def test_no_pin_is_driven_before_a_reading_informs_a_decision(tmp_path):
    """Every load's de-energized state is its fail-safe, and that is the
    state the machine is already in while the controller is not running. So
    acting ahead of information buys nothing and costs a recorder cycle on
    every start."""
    machine, pin, _, _ = build(tmp_path, readings={})
    machine.once()
    assert pin.levels == [], "a relay moved with no reading in hand"


def test_the_first_informed_pass_drives_the_pin_once(tmp_path):
    machine, pin, _, _ = build(tmp_path)
    machine.once()
    assert pin.levels == [False], "connected drives this polarity low"


def test_a_held_load_is_driven_again_on_every_later_pass(tmp_path):
    """Within one boot the loop's state survives a crash and the pin does
    not, because the line is released when the process dies. A restarted loop
    must put the load back where it had it rather than wait for fresh news."""
    machine, pin, _, state = build(tmp_path)
    machine.once()
    state["readings"] = {}
    state["mono"] = 100.0
    machine.once()
    assert len(pin.levels) == 2, "stopped driving when the reading went away"


# -- the decision reaching the pin ------------------------------------------

def test_a_shed_decision_drives_the_pin_high_on_this_polarity(tmp_path):
    """The level, not the intention. A test on the decision passes while the
    pin drives the other way and the cameras run all night."""
    machine, pin, _, _ = build(
        tmp_path, readings=reading(soc=5.0),
        at=datetime.datetime(2026, 6, 21, 12, tzinfo=ZONE))
    machine.once()
    assert pin.levels == [True]


def test_the_floor_reaches_the_pin(tmp_path):
    machine, pin, published, _ = build(tmp_path,
                                       readings=reading(soc=100.0, cell=3.10))
    machine.once()
    assert pin.levels == [True]
    assert published["loads"]["cameras"]["reason_code"] == 2.0


# -- the clock --------------------------------------------------------------

def test_an_untrusted_clock_suspends_the_schedule_and_says_so(tmp_path):
    machine, pin, published, _ = build(
        tmp_path, readings=reading(soc=45.0), trusted=False,
        at=datetime.datetime(2026, 6, 21, 23, tzinfo=ZONE))
    machine.once()
    assert published["fields"]["clock_synced"] == 0.0
    assert published["loads"]["cameras"]["schedule_suspended"] == 1.0
    assert pin.levels == [False], "the cameras run, which is also fail-safe"


def test_the_high_water_mark_is_advanced_only_from_a_trusted_clock(tmp_path):
    path = str(tmp_path / "hw.json")

    machine, _, _, _ = build(tmp_path, trusted=False)
    machine.once()
    assert files.read_high_water(path) is None, (
        "an untrusted clock set the bound that is meant to catch it")

    machine, _, _, _ = build(tmp_path, trusted=True)
    machine.once()
    assert files.read_high_water(path) is not None


def test_a_place_with_no_sunrise_suspends_the_schedule_rather_than_crashing(
        tmp_path):
    """Impossible where this runs. A schedule that cannot be placed must not
    be applied, and the loop must survive to keep the floor working."""
    machine, pin, published, _ = build(tmp_path, readings=reading(soc=45.0),
                                       at=datetime.datetime(2026, 6, 21, 23,
                                                            tzinfo=ZONE))
    machine.location = {"latitude": 85.0, "longitude": -97.4}
    machine.once()
    assert published["fields"]["clock_synced"] == 0.0
    assert "does not rise" in published["fields"]["clock_reason"]
    assert pin.levels == [False]


# -- what it publishes ------------------------------------------------------

#: The fields every load's sub-document carries, and the ones only a value
#: in hand can add. Pinned here so that a field quietly renamed, or one
#: quietly dropped, fails against this list rather than against the loop.
LOAD_FIELDS = {"powered", "level", "shed_level", "reason_code", "reason",
               "schedule_suspended", "floor_latched", "level_changes"}
LOAD_FIELDS_WITH_A_VALUE = {"decided_on"}
LOAD_FIELDS_WHILE_THE_FLOOR_HOLDS = {"floor_latched_by"}
LOOP_FIELDS = {"clock_synced", "clock_reason", "passes", "loop_monotonic"}

#: The text fields. Everything else is a float.
TEXT_FIELDS = {"clock_reason", "reason", "floor_latched_by"}

#: How the loop names the source holding a floor: the device, a dot, the
#: field. Pinned as the literal an operator would read off a dashboard.
CELL_SOURCE = "bms.cell_min_volts"


def test_each_load_is_published_under_its_own_name(tmp_path):
    """One sub-document per load, with the field names bare, so a reader
    tags each one with the load instead of parsing a prefix off every field
    and a chart repeats over loads without naming any."""
    machine, _, published, _ = build(tmp_path)
    machine.once()
    assert set(published["fields"]) == LOOP_FIELDS
    assert set(published["loads"]) == {"cameras"}
    assert set(published["loads"]["cameras"]) == (
        LOAD_FIELDS | LOAD_FIELDS_WITH_A_VALUE)


def test_decided_on_is_absent_rather_than_zero_without_a_value(tmp_path):
    """A zero would read as a battery at zero percent."""
    machine, _, published, _ = build(tmp_path, readings={})
    machine.once()
    assert set(published["loads"]["cameras"]) == LOAD_FIELDS


@pytest.mark.parametrize("readings", [reading(), reading(cell=3.10)],
                         ids=["healthy", "floored"])
def test_every_published_number_is_a_float(tmp_path, readings):
    """InfluxDB refuses a field whose type changes between writes."""
    machine, _, published, _ = build(tmp_path, readings=readings)
    machine.once()
    everything = [published["fields"]] + list(published["loads"].values())
    for fields in everything:
        for name, value in fields.items():
            assert isinstance(value, (float, str)), name
            assert isinstance(value, str) == (name in TEXT_FIELDS), name


def test_the_floor_latch_and_its_owner_are_published_while_it_holds(tmp_path):
    """The latch outlives the reading that tripped it: it holds between the
    shedding bound and the restoring bound, and only the source that tripped
    it may release it. Both are published so a dashboard can say which
    number has to recover, not just that the cameras are off."""
    machine, _, published, state = build(tmp_path, readings=reading(cell=3.10))
    machine.once()
    cameras = published["loads"]["cameras"]
    assert cameras["floor_latched"] == 1.0
    assert cameras["floor_latched_by"] == CELL_SOURCE
    assert set(cameras) == (LOAD_FIELDS | LOAD_FIELDS_WITH_A_VALUE
                            | LOAD_FIELDS_WHILE_THE_FLOOR_HOLDS)

    state["readings"] = reading(cell=3.20)
    state["mono"] = 30.0
    machine.once()
    cameras = published["loads"]["cameras"]
    assert cameras["floor_latched"] == 1.0, "between the bounds it holds"
    assert cameras["floor_latched_by"] == CELL_SOURCE

    state["readings"] = reading(cell=3.30)
    state["mono"] = 60.0
    machine.once()
    cameras = published["loads"]["cameras"]
    assert cameras["floor_latched"] == 0.0
    assert "floor_latched_by" not in cameras


def test_the_floor_owner_is_absent_rather_than_blank_when_nothing_holds_it(
        tmp_path):
    """No source holds a latch that is not latched. A blank would be a value
    that names no device, and a query would have to know to ignore it."""
    machine, _, published, _ = build(tmp_path)
    machine.once()
    cameras = published["loads"]["cameras"]
    assert cameras["floor_latched"] == 0.0
    assert "floor_latched_by" not in cameras


def test_the_published_level_is_what_went_out_not_what_was_wanted(tmp_path):
    """On this polarity they are opposites, so a field that reported the wish
    would read backwards every time."""
    machine, _, published, _ = build(tmp_path)
    machine.once()
    assert published["loads"]["cameras"]["powered"] == 1.0
    assert published["loads"]["cameras"]["level"] == 0.0


def test_the_pass_count_advances_so_a_wedged_loop_is_visible(tmp_path):
    """Every other field would sit at its last value and look healthy."""
    machine, _, published, state = build(tmp_path)
    machine.once()
    first = published["fields"]["passes"]
    state["mono"] = 30.0
    machine.once()
    assert published["fields"]["passes"] > first


def test_a_failure_to_publish_does_not_stop_it_controlling(tmp_path):
    """Publishing is how somebody finds out. Controlling is the job."""
    pin = relay.Pretend(23, relay.ENERGIZE_TO_SHED)

    def explode(_path, _data):
        raise OSError("no space")

    machine = loop.Loop(
        FakeNode({"cameras": CAMERAS}), LOCATION, ZONE,
        relays={"cameras": pin},
        now=lambda: datetime.datetime(2026, 6, 21, 12, tzinfo=ZONE),
        monotonic=lambda: 0.0,
        trust=lambda high_water=None: clock.Trust(True, "under test"),
        readings=lambda: reading(soc=5.0),
        switches=lambda: {"shedding_enabled": True, "loads": {}},
        state_path=str(tmp_path / "s.json"),
        high_water_path=str(tmp_path / "hw.json"),
        publish=explode, boot=BOOT)
    machine.once()
    assert pin.levels == [True], "a publishing failure cost a shed"


# -- counting level changes -----------------------------------------------

def level_changes(published):
    return published["loads"]["cameras"]["level_changes"]


def test_a_first_drive_that_leaves_the_pin_de_energized_is_not_a_level_change(
        tmp_path):
    """Until the first drive the pin sits at its de-energized level, which
    is where the machine left it, so driving it there again changes nothing
    about the load."""
    machine, pin, published, _ = build(tmp_path)
    machine.once()
    assert pin.levels == [False]
    assert level_changes(published) == 0.0


def test_a_first_drive_that_energizes_the_relay_is_a_level_change(tmp_path):
    """The relay was de-energized until that drive, so the load changed
    state, and on the camera branch that is a recorder power cycle. Not
    counting it would under-report by one every boot, in exactly the case
    the counter exists for."""
    machine, pin, published, _ = build(tmp_path, readings=reading(soc=5.0))
    machine.once()
    assert pin.levels == [True]
    assert level_changes(published) == 1.0


def test_each_change_of_the_driven_level_counts_and_a_hold_does_not(tmp_path):
    machine, pin, published, state = build(tmp_path)
    seen = []
    for step, soc in enumerate((60.0, 5.0, 5.0, 60.0, 5.0)):
        state["readings"] = reading(soc=soc)
        state["mono"] = step * 100.0
        machine.once()
        seen.append(level_changes(published))
    assert pin.levels == [False, True, True, False, True]
    assert seen == [0.0, 1.0, 1.0, 2.0, 3.0]


def test_a_commanded_change_that_never_reached_the_pin_is_not_a_level_change(
        tmp_path):
    """A load is not driven until a reading informs a decision about it. An
    operator forcing it off and then clearing that, before any reading has
    arrived, changes what was commanded twice and the pin not at all."""
    switches = {"shedding_enabled": True,
                "loads": {"cameras": {"force": "off",
                                      "expires_epoch": 4_000_000_000.0}}}
    machine, pin, published, state = build(tmp_path, readings={},
                                           switches=switches)
    machine.once()
    assert published["loads"]["cameras"]["powered"] == 0.0

    switches["loads"].clear()
    state["mono"] = 100.0
    machine.once()
    assert published["loads"]["cameras"]["powered"] == 1.0
    assert pin.levels == [], "nothing was driven without a reading"

    state["readings"] = reading()
    state["mono"] = 200.0
    machine.once()
    assert pin.levels == [False]
    assert level_changes(published) == 0.0


def test_a_dry_run_counts_no_level_change(tmp_path):
    machine, pin, published, _ = build(tmp_path, readings=reading(soc=5.0))
    machine.once(apply=False)
    assert pin.levels == []
    assert level_changes(published) == 0.0


def test_the_count_survives_a_restart_within_a_boot_and_not_a_new_boot(
        tmp_path):
    """Kept with the rest of the loop's state, in tmpfs. A restarted loop
    that left the relay de-energized finds it there and puts it back
    there, which changes nothing, and a new boot starts from nothing."""
    machine, _, published, state = build(tmp_path, readings=reading(soc=5.0))
    machine.once()
    state["readings"] = reading(soc=60.0)
    state["mono"] = 100.0
    machine.once()
    assert level_changes(published) == 2.0

    resumed, pin, published, _ = build(tmp_path, readings=reading(soc=60.0),
                                       mono=200.0)
    resumed.once()
    assert pin.levels == [False], "put back where it was"
    assert level_changes(published) == 2.0

    rebooted, pin, published, _ = build(tmp_path, readings=reading(soc=5.0),
                                        boot="the-next-boot")
    rebooted.once()
    assert pin.levels == [True]
    assert level_changes(published) == 1.0, "counted afresh from zero"


def test_a_restart_that_finds_the_relay_dropped_counts_the_drop_and_the_return(
        tmp_path):
    """The line is released when a process ends, however it ends, so a relay
    left energized drops before the next process can put it back. On the
    camera branch that is the recorder powered and then cut again: two
    changes of level, both of which happened to the load."""
    machine, _, published, _ = build(tmp_path, readings=reading(soc=5.0))
    machine.once()
    assert level_changes(published) == 1.0

    resumed, pin, published, _ = build(tmp_path, readings=reading(soc=5.0),
                                       mono=100.0)
    resumed.once()
    assert pin.levels == [True], "put back where it was"
    assert level_changes(published) == 3.0

    resumed.once()
    assert level_changes(published) == 3.0, "the drop is counted once"


class StuckRelay:
    """A relay whose line cannot be driven."""

    def apply(self, connected):
        raise relay.RelayError("cannot drive GPIO24: under test")

    def close(self):
        pass


def two_loads(tmp_path, cameras=None, recorder=None, soc=5.0, mono=0.0,
              state_path=None):
    """Cameras on GPIO23 and a recorder on GPIO24, decided in that order,
    either of whose relays may be one that cannot be driven."""
    cameras = cameras or relay.Pretend(23, relay.ENERGIZE_TO_SHED)
    recorder = recorder or relay.Pretend(24, relay.ENERGIZE_TO_SHED)
    published = {}
    machine = loop.Loop(
        FakeNode({"cameras": CAMERAS,
                  "recorder": dict(CAMERAS, control_pin=24, min_off="10m")}),
        LOCATION, ZONE,
        relays={"cameras": cameras, "recorder": recorder},
        now=lambda: datetime.datetime(2026, 6, 21, 12, tzinfo=ZONE),
        monotonic=lambda: mono,
        trust=lambda high_water=None: clock.Trust(True, "under test"),
        readings=lambda: reading(soc=soc),
        switches=lambda: {"shedding_enabled": True, "loads": {}},
        state_path=state_path or str(tmp_path / "state.json"),
        published_path=str(tmp_path / "published.json"),
        high_water_path=str(tmp_path / "hw.json"),
        publish=lambda _path, data: published.update(data),
        boot=BOOT)
    return machine, cameras, recorder, published


def test_a_pass_ended_by_a_relay_fault_keeps_what_the_other_pins_did(
        tmp_path):
    """A pin that cannot be driven ends the process, and the supervisor
    starts another. The pins driven before it in that pass did change, and
    the process ending released them, so the next process has to know
    where they were left."""
    machine, cameras, _, _ = two_loads(tmp_path, recorder=StuckRelay())
    with pytest.raises(relay.RelayError):
        machine.once()
    assert cameras.levels == [True]

    resumed, cameras, _, published = two_loads(tmp_path, mono=100.0)
    resumed.once()
    assert cameras.levels == [True]
    assert published["loads"]["cameras"]["level_changes"] == 3.0


def test_a_load_a_relay_fault_stopped_short_of_keeps_no_command(tmp_path):
    """The recorder was commanded off and never driven. Keeping that command
    would start its minimum off time on a change that never happened, and
    hold it off after the battery had recovered."""
    machine, _, _, _ = two_loads(tmp_path, soc=60.0)
    machine.once()

    failing, _, _, _ = two_loads(tmp_path, cameras=StuckRelay(), soc=5.0,
                                 mono=100.0)
    with pytest.raises(relay.RelayError):
        failing.once()

    resumed, _, recorder, published = two_loads(tmp_path, soc=60.0,
                                                mono=200.0)
    resumed.once()
    assert recorder.levels == [False], "the recorder runs"
    assert published["loads"]["recorder"]["powered"] == 1.0


def test_a_drive_that_fails_counts_no_change_however_often_it_is_retried(
        tmp_path):
    """The relay dropped once, when the process that energized it ended.
    Each later process that fails to drive it changes nothing."""
    machine, cameras, _, _ = two_loads(tmp_path)
    machine.once()
    assert cameras.levels == [True]

    for mono in (100.0, 200.0):
        failing, _, _, _ = two_loads(tmp_path, cameras=StuckRelay(),
                                     mono=mono)
        with pytest.raises(relay.RelayError):
            failing.once()

    resumed, cameras, _, published = two_loads(tmp_path, mono=300.0)
    resumed.once()
    assert cameras.levels == [True]
    assert published["loads"]["cameras"]["level_changes"] == 3.0


def test_a_relay_fault_is_reported_even_when_the_state_cannot_be_saved(
        tmp_path, monkeypatch):
    attempts = []

    def refuse(*args, **kwargs):
        attempts.append(args)
        raise OSError("read-only file system")

    monkeypatch.setattr(files, "write_state", refuse)
    machine, _, _, _ = two_loads(tmp_path, recorder=StuckRelay())
    with pytest.raises(relay.RelayError):
        machine.once()
    assert len(attempts) == 1, "the state was offered for saving"


# -- state across passes ----------------------------------------------------

def test_latches_survive_between_passes(tmp_path):
    """Written out and read back, not merely held in memory, because the
    process is restarted by its supervisor and the ratchet has to survive."""
    machine, _, _, state = build(tmp_path, readings=reading(soc=45.0))
    machine.once()
    assert files.read_state(str(tmp_path / "state.json"),
                            this_boot=BOOT) != {}

    resumed, pin, _, _ = build(tmp_path, readings=reading(soc=45.0))
    assert resumed.state["cameras"]["latched"]["0"] is True


# -- refusing to start ------------------------------------------------------

def test_a_bad_control_block_refuses_to_start(tmp_path):
    """Relays stay released, which is every load's fail-safe. A controller
    that started on a block it could not read would drive pins from
    defaults nobody wrote."""
    with pytest.raises(SpecError):
        loop.Loop(FakeNode({"cameras": dict(CAMERAS, polarity="sideways")}),
                  LOCATION, ZONE, relays={}, boot=BOOT)


def test_a_loop_with_no_boot_identity_refuses_to_start(tmp_path):
    """Every stamp it keeps is monotonic, and the identity is what makes a
    stored one comparable with a current one. Without it the state file is
    discarded on every read and the tier ratchet silently stops existing."""
    with pytest.raises(loop.LoopError) as raised:
        loop.Loop(FakeNode({"cameras": CAMERAS}), LOCATION, ZONE,
                  relays={}, boot="")
    assert "ratchet" in str(raised.value)


def test_a_dry_run_touches_no_pin(tmp_path):
    machine, pin, published, _ = build(tmp_path, readings=reading(soc=5.0))
    machine.once(apply=False)
    assert pin.levels == []
    assert published["loads"]["cameras"]["powered"] == 0.0, (
        "a dry run must still say what it would have done")


# -- a CPU fan ----------------------------------------------------------------

FAN = {"type": "cpu_fan", "enabled": True, "control_pin": 24,
       "polarity": "energize_to_connect",
       "source": {"device": "host", "field": "cpu_temp_f"},
       "stale_after": "2m", "run_above_f": 165, "stop_below_f": 145}


def test_the_cpu_fan_pin_follows_the_chip_temperature(tmp_path):
    pin = relay.Pretend(24, relay.ENERGIZE_TO_CONNECT)
    world = {"temp": 170.0}
    machine = loop.Loop(
        FakeNode({"cpu_fan": FAN}, devices=("host",)), LOCATION, ZONE,
        relays={"cpu_fan": pin},
        now=lambda: datetime.datetime(2026, 6, 21, 12, tzinfo=ZONE),
        monotonic=lambda: 0.0,
        trust=lambda high_water=None: clock.Trust(True, "under test"),
        readings=lambda: {"host": {"fields": {"cpu_temp_f": world["temp"]},
                                   "age_seconds": 1.0}},
        switches=lambda: {"shedding_enabled": True, "loads": {}},
        state_path=str(tmp_path / "state.json"),
        published_path=str(tmp_path / "published.json"),
        high_water_path=str(tmp_path / "hw.json"),
        publish=lambda _path, _data: None, boot=BOOT)
    machine.once()
    world["temp"] = 155.0
    machine.once()
    world["temp"] = 140.0
    machine.once()
    assert pin.levels == [True, True, False]
