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
          switches=None, loads=None):
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
        boot=BOOT)
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
    assert published["fields"]["cameras_reason_code"] == 2.0


# -- the clock --------------------------------------------------------------

def test_an_untrusted_clock_suspends_the_schedule_and_says_so(tmp_path):
    machine, pin, published, _ = build(
        tmp_path, readings=reading(soc=45.0), trusted=False,
        at=datetime.datetime(2026, 6, 21, 23, tzinfo=ZONE))
    machine.once()
    assert published["fields"]["clock_synced"] == 0.0
    assert published["fields"]["cameras_schedule_suspended"] == 1.0
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

def test_every_published_number_is_a_float(tmp_path):
    """InfluxDB refuses a field whose type changes between writes."""
    machine, _, published, _ = build(tmp_path)
    machine.once()
    for name, value in published["fields"].items():
        assert isinstance(value, (float, str)), name
        if isinstance(value, str):
            assert name.endswith("_reason"), name


def test_the_published_level_is_what_went_out_not_what_was_wanted(tmp_path):
    """On this polarity they are opposites, so a field that reported the wish
    would read backwards every time."""
    machine, _, published, _ = build(tmp_path)
    machine.once()
    assert published["fields"]["cameras_powered"] == 1.0
    assert published["fields"]["cameras_level"] == 0.0


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
    assert published["fields"]["cameras_powered"] == 0.0, (
        "a dry run must still say what it would have done")
