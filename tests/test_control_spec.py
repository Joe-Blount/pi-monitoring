"""Reading a control block, and every way it is refused.

A node file is edited by hand about a machine an hour away. Each refusal here
exists because the alternative is a relay doing the opposite of what somebody
meant, found weeks later from a gap in the data.
"""

import datetime

import pytest

from monitoring.control import spec


class FakeDevice:
    def __init__(self, name, enabled=True):
        self.name = name
        self.enabled = enabled


class FakeNode:
    def __init__(self, loads, devices=("bms", "mppt"), disabled=()):
        self.devices = [FakeDevice(name) for name in devices]
        self.devices += [FakeDevice(name, enabled=False) for name in disabled]
        self.control = {"loads": loads}


LADDER = {
    "source": {"device": "bms", "field": "state_of_charge_pct"},
    "stale_after": "10m",
    "release_margin_pct": 15,
    "enter_after_readings": 2,
    "restore_dwell": "15m",
    "floor": {"sources": [
        {"device": "bms", "field": "cell_min_volts",
         "shed_below": 3.15, "restore_above": 3.25},
    ]},
    "tiers": [
        {"below_pct": 50, "schedule": {"type": "clock",
                                       "on_from": "05:00", "on_to": "22:00"}},
        {"below_pct": 25, "schedule": {
            "type": "solar",
            "on_windows": [
                {"from": ["sunrise", "-1h"], "to": ["sunrise", "2h"]},
                {"from": ["sunset", "-2h"], "to": ["sunset", "1h"]},
            ]}},
        {"below_pct": 10, "schedule": {"type": "never"}},
    ],
}

CAMERAS = {
    "type": "dc_branch",
    "enabled": True,
    "control_pin": 23,
    "polarity": "energize_to_shed",
    "min_run": "10m",
    "min_off": "30m",
    "shed_ladder": LADDER,
}


def parse(load=None, **changes):
    merged = dict(load or CAMERAS)
    merged.update(changes)
    return spec.load_spec("cameras", merged, {"bms", "mppt"})


def with_ladder(**changes):
    ladder = dict(LADDER)
    ladder.update(changes)
    return parse(shed_ladder=ladder)


def refused(load=None, **changes):
    with pytest.raises(spec.SpecError) as raised:
        parse(load, **changes)
    return str(raised.value)


# -- the shipped shape ------------------------------------------------------

def test_the_shipped_camera_load_parses():
    out = parse()
    assert out["control_pin"] == 23
    assert out["polarity"] == "energize_to_shed"
    assert out["min_off"] == 1800.0
    ladder = out["shed_ladder"]
    assert [tier["below_pct"] for tier in ladder["tiers"]] == [50.0, 25.0, 10.0]
    assert ladder["stale_after"] == 600.0
    assert ladder["restore_dwell"] == 900.0
    assert ladder["floor"]["sources"][0]["shed_below"] == 3.15


def test_times_and_durations_are_converted_not_passed_through():
    """The decision compares minutes and seconds. A string arriving there
    would raise inside the loop rather than be refused at the edge."""
    ladder = parse()["shed_ladder"]
    assert ladder["tiers"][0]["schedule"]["on_from"] == 300
    assert ladder["tiers"][0]["schedule"]["on_to"] == 1320
    # A solar offset comes out as a timedelta, not a number of seconds,
    # because the decision adds it to a datetime. Every other duration here
    # stays a float, since those are compared against a monotonic clock.
    window = ladder["tiers"][1]["schedule"]["on_windows"][0]
    assert window["from"] == ("sunrise", datetime.timedelta(hours=-1))
    assert window["to"] == ("sunrise", datetime.timedelta(hours=2))


# -- a typo must not read as a comment --------------------------------------

@pytest.mark.parametrize("where,change", [
    ("load", {"min_offf": "30m"}),
    ("load", {"polarityy": "energize_to_shed"}),
])
def test_an_unknown_key_on_a_load_is_refused(where, change):
    """The whole reason this module refuses rather than defaults.

    A misspelled threshold leaves the default in place, the load behaves as
    though the line was never written, and the file reads as though it was.
    """
    message = refused(**change)
    assert "unknown key" in message
    assert "reads as though it did not" in message


def test_an_unknown_key_inside_the_ladder_is_refused():
    assert "unknown key" in refused(shed_ladder=dict(LADDER, margin=15))


def test_an_unknown_key_inside_a_tier_is_refused():
    broken = dict(LADDER)
    broken["tiers"] = [dict(LADDER["tiers"][0], below=50)]
    assert "unknown key" in refused(shed_ladder=broken)


def test_an_unknown_key_inside_a_schedule_is_refused():
    broken = dict(LADDER)
    broken["tiers"] = [{"below_pct": 50,
                        "schedule": {"type": "clock", "on_from": "05:00",
                                     "on_to": "22:00", "on_until": "23:00"}}]
    assert "unknown key" in refused(shed_ladder=broken)


def test_an_unknown_key_inside_a_floor_source_is_refused():
    broken = dict(LADDER)
    broken["floor"] = {"sources": [
        {"device": "bms", "field": "cell_min_volts",
         "shed_below": 3.15, "restore_above": 3.25, "hysteresis": 0.1}]}
    assert "unknown key" in refused(shed_ladder=broken)


# -- the ladder must descend ------------------------------------------------

@pytest.mark.parametrize("values", [
    [50, 25, 30],
    [25, 50],
    [50, 50],
])
def test_tiers_must_descend_strictly(values):
    """Everything that reasons about depth depends on the latched set being a
    prefix of the ladder, and that holds only because the tiers descend
    strictly and share one margin."""
    broken = dict(LADDER)
    broken["tiers"] = [{"below_pct": value, "schedule": {"type": "never"}}
                       for value in values]
    assert "descend strictly" in refused(shed_ladder=broken)


def test_a_release_margin_that_coincides_with_the_next_entry_is_allowed():
    """With a margin of fifteen the ten tier releases above twenty-five and
    the twenty-five tier enters below it. That is the shipped ladder and the
    conservative answer at the boundary, so the validator must permit it."""
    assert with_ladder(release_margin_pct=15)["shed_ladder"][
        "release_margin_pct"] == 15.0


def test_a_margin_of_zero_is_refused():
    assert "chatters" in refused(shed_ladder=dict(LADDER,
                                                  release_margin_pct=0))


@pytest.mark.parametrize("value", [0, -1, 1.5, True, "two"])
def test_enter_after_readings_must_be_a_whole_number_of_at_least_one(value):
    assert "whole number" in refused(
        shed_ladder=dict(LADDER, enter_after_readings=value))


# -- sources ----------------------------------------------------------------

def test_a_source_naming_an_undeclared_device_is_refused():
    """The failure this replaces was silent: nothing read the device, the
    ladder held its default forever, and a probe reported every device fine."""
    message = refused(shed_ladder=dict(
        LADDER, source={"device": "shunt", "field": "state_of_charge_pct"}))
    assert "does not declare" in message
    assert "bms" in message


def test_a_floor_source_naming_an_undeclared_device_is_refused():
    broken = dict(LADDER)
    broken["floor"] = {"sources": [{"device": "nope", "field": "v",
                                    "shed_below": 1, "restore_above": 2}]}
    assert "does not declare" in refused(shed_ladder=broken)


# -- the floor --------------------------------------------------------------

@pytest.mark.parametrize("missing", ["shed_below", "restore_above"])
def test_a_floor_source_needs_both_bounds(missing):
    """One bound is a threshold that chatters, not a floor."""
    source = {"device": "bms", "field": "cell_min_volts",
              "shed_below": 3.15, "restore_above": 3.25}
    del source[missing]
    broken = dict(LADDER, floor={"sources": [source]})
    assert missing in refused(shed_ladder=broken)


@pytest.mark.parametrize("shed,restore", [(3.25, 3.15), (3.15, 3.15)])
def test_a_floor_that_can_never_release_is_refused(shed, restore):
    broken = dict(LADDER, floor={"sources": [
        {"device": "bms", "field": "cell_min_volts",
         "shed_below": shed, "restore_above": restore}]})
    assert "can never release" in refused(shed_ladder=broken)


def test_a_floor_with_no_sources_is_refused():
    assert "at least one source" in refused(
        shed_ladder=dict(LADDER, floor={"sources": []}))


def test_a_ladder_may_have_no_floor_at_all():
    ladder = dict(LADDER)
    del ladder["floor"]
    assert parse(shed_ladder=ladder)["shed_ladder"]["floor"] is None


# -- schedules --------------------------------------------------------------

def test_an_unknown_schedule_type_is_refused():
    broken = dict(LADDER, tiers=[{"below_pct": 50,
                                  "schedule": {"type": "whenever"}}])
    assert "type must be one of" in refused(shed_ladder=broken)


def test_a_clock_schedule_needs_both_ends():
    broken = dict(LADDER, tiers=[{"below_pct": 50,
                                  "schedule": {"type": "clock",
                                               "on_from": "05:00"}}])
    assert "needs on_to" in refused(shed_ladder=broken)


def test_a_clock_schedule_whose_ends_coincide_is_refused():
    """Either a whole day or none of it, and the file does not say which."""
    broken = dict(LADDER, tiers=[{"below_pct": 50,
                                  "schedule": {"type": "clock",
                                               "on_from": "05:00",
                                               "on_to": "05:00"}}])
    assert "whole day or none of it" in refused(shed_ladder=broken)


def test_a_clock_window_may_wrap_midnight():
    broken = dict(LADDER, tiers=[{"below_pct": 50,
                                  "schedule": {"type": "clock",
                                               "on_from": "22:00",
                                               "on_to": "05:00"}}])
    schedule = parse(shed_ladder=broken)["shed_ladder"]["tiers"][0]["schedule"]
    assert schedule["on_from"] == 1320 and schedule["on_to"] == 300


@pytest.mark.parametrize("value", ["5pm", "25:00", "noon", 500, "05:0a"])
def test_a_time_that_is_not_a_time_is_refused(value):
    broken = dict(LADDER, tiers=[{"below_pct": 50,
                                  "schedule": {"type": "clock",
                                               "on_from": value,
                                               "on_to": "22:00"}}])
    assert refused(shed_ladder=broken)


def test_a_solar_window_naming_an_event_that_does_not_exist_is_refused():
    broken = dict(LADDER, tiers=[{"below_pct": 50, "schedule": {
        "type": "solar",
        "on_windows": [{"from": ["dawn", "-1h"], "to": ["sunrise", "1h"]}]}}])
    assert "it must be one of" in refused(shed_ladder=broken)


def test_a_solar_window_needs_both_edges():
    broken = dict(LADDER, tiers=[{"below_pct": 50, "schedule": {
        "type": "solar",
        "on_windows": [{"from": ["sunrise", "-1h"]}]}}])
    assert "needs a to" in refused(shed_ladder=broken)


def test_a_never_schedule_takes_nothing_else():
    """Keys beside it would read as though they were in force."""
    broken = dict(LADDER, tiers=[{"below_pct": 50,
                                  "schedule": {"type": "never",
                                               "on_from": "05:00"}}])
    assert refused(shed_ladder=broken)


def test_a_clock_schedule_carrying_solar_windows_is_refused():
    broken = dict(LADDER, tiers=[{"below_pct": 50, "schedule": {
        "type": "clock", "on_from": "05:00", "on_to": "22:00",
        "on_windows": [{"from": ["sunrise", "-1h"],
                        "to": ["sunrise", "1h"]}]}}])
    assert "no on_windows" in refused(shed_ladder=broken)


# -- polarity and the pin ---------------------------------------------------

@pytest.mark.parametrize("value", [None, "", "normally_closed", "shed"])
def test_polarity_must_be_one_of_the_two_names(value):
    """It decides what a dead controller leaves behind, and the two loads
    here want opposite answers, so there is no safe default."""
    assert "no safe default" in refused(polarity=value)


@pytest.mark.parametrize("value", [None, "23", 23.0, True])
def test_a_control_pin_must_be_a_whole_number(value):
    assert "control_pin" in refused(control_pin=value)


def test_a_load_with_no_ladder_is_refused():
    broken = dict(CAMERAS)
    del broken["shed_ladder"]
    with pytest.raises(spec.SpecError) as raised:
        spec.load_spec("cameras", broken, {"bms"})
    assert "needs a shed_ladder" in str(raised.value)


# -- types ------------------------------------------------------------------

def test_an_enabled_deferred_load_is_refused_rather_than_skipped():
    """Skipping it at run time leaves a file saying the load is enabled and
    nothing driving it, which reads exactly like a load that works."""
    message = refused(type="sequenced_ac")
    assert "enabled: false" in message
    assert "indistinguishable" in message


def test_a_disabled_deferred_load_is_accepted_and_not_driven():
    assert spec.load_spec("irrigation",
                          {"type": "sequenced_ac", "enabled": False},
                          {"bms"}) is None


def test_a_load_type_nothing_drives_is_refused():
    assert "not a type this controller drives" in refused(type="vent_fan")


def test_a_disabled_load_is_not_returned():
    assert parse(enabled=False) is None


def test_a_ladder_naming_a_disabled_device_is_refused():
    """A disabled device never writes the cache, so no tier would latch and
    the controller would run and do nothing, which is the silent failure
    this module is written against."""
    node = FakeNode({"cameras": dict(CAMERAS, shed_ladder=dict(
        LADDER, source={"device": "shunt",
                        "field": "state_of_charge_pct"}))},
        disabled=("shunt",))
    with pytest.raises(spec.SpecError) as raised:
        spec.load_control(node)
    assert "declared and disabled" in str(raised.value)


@pytest.mark.parametrize("value", ["false", "no", 0, 1, None])
def test_enabled_must_be_a_yes_or_a_no(value):
    """The rule the device loader already applies. A loose test is the wrong
    way round for a setting whose purpose is to stop something: the string
    "false" is truthy and would read as yes."""
    assert "true or false" in refused(enabled=value)


@pytest.mark.parametrize("key", ["readback", "on_stale_data"])
def test_a_key_nothing_reads_yet_is_refused(key):
    """Both are specified in the node files and nothing acts on either. A
    key accepted and unread sits in the file looking like it is in force,
    which is the exact failure this module exists to prevent."""
    assert "unknown key" in refused(**{key: "whatever"})


def test_an_unknown_key_on_the_control_block_itself_is_refused():
    """These belong to the loop rather than to a load, and a misspelled
    loop_interval would silently leave the default in place."""
    node = FakeNode({"cameras": CAMERAS})
    node.control["loop_intervall"] = "30s"
    with pytest.raises(spec.SpecError) as raised:
        spec.load_control(node)
    assert "unknown key" in str(raised.value)


def test_a_restore_dwell_is_required_rather_than_defaulted():
    """Entry needs consecutive readings so one glitched sample cannot latch
    a tier. Release with no dwell lets one glitched sample clear every tier
    at once, and the minimum run time then extends the mistake. A file that
    wants none can say zero and mean it."""
    ladder = dict(LADDER)
    del ladder["restore_dwell"]
    assert "needs restore_dwell" in refused(shed_ladder=ladder)
    assert with_ladder(restore_dwell=0)["shed_ladder"]["restore_dwell"] == 0.0


@pytest.mark.parametrize("value", ["0s", "-5m"])
def test_a_staleness_limit_must_be_positive(value):
    """Zero or negative makes every reading stale forever, so nothing ever
    latches and nothing is ever published as a reason."""
    assert refused(shed_ladder=dict(LADDER, stale_after=value))


def test_a_negative_minimum_time_is_refused():
    assert "cannot be negative" in refused(min_off="-30m")


@pytest.mark.parametrize("value", ["12:75", "23:60", "25:00", "-1:00"])
def test_each_part_of_a_time_is_range_checked(value):
    """Checking only the total accepts 12:75 as 13:15, which is a typo read
    as a time."""
    broken = dict(LADDER, tiers=[{"below_pct": 50,
                                  "schedule": {"type": "clock",
                                               "on_from": value,
                                               "on_to": "22:00"}}])
    assert refused(shed_ladder=broken)


def test_a_solar_window_that_ends_before_it_starts_is_refused():
    """Measured from the same event, so the comparison is exact. Such a
    window is never on, and the tier reads as scheduled while behaving as
    never."""
    broken = dict(LADDER, tiers=[{"below_pct": 50, "schedule": {
        "type": "solar",
        "on_windows": [{"from": ["sunrise", "2h"],
                        "to": ["sunrise", "-1h"]}]}}])
    assert "never be on" in refused(shed_ladder=broken)


@pytest.mark.parametrize("value", [0, -5, 101, 500])
def test_below_pct_must_be_a_percentage(value):
    broken = dict(LADDER, tiers=[{"below_pct": value,
                                  "schedule": {"type": "never"}}])
    assert "must be a percentage" in refused(shed_ladder=broken)


def test_a_floor_present_but_empty_is_refused():
    """The likeliest cause is a half-deleted floor, and silently dropping
    the only rule with nothing above it is the wrong way to resolve that."""
    assert refused(shed_ladder=dict(LADDER, floor={}))


def test_a_disabled_deferred_load_keeps_its_specification():
    """The whole point of deferring rather than deleting. Checking keys
    before the type would refuse the irrigation block for declaring an
    inverter and a pump, and the only accepted form would be two lines,
    which deletes the work."""
    assert spec.load_spec("irrigation", {
        "type": "sequenced_ac",
        "enabled": False,
        "inverter": {"control_pin": 25, "polarity": "energize_to_connect"},
        "pump": {"control_pin": 22, "polarity": "energize_to_connect"},
        "guard": {"absolute_max_run": "60m"},
        "limits": {"cooldown": "2h"},
    }, {"bms"}) is None


# -- the whole block --------------------------------------------------------

def test_a_node_yields_only_the_loads_to_drive():
    node = FakeNode({"cameras": CAMERAS,
                     "irrigation": {"type": "sequenced_ac", "enabled": False},
                     "spare": dict(CAMERAS, enabled=False)})
    out = spec.load_control(node)
    assert sorted(out) == ["cameras"]


def test_one_unreadable_load_refuses_the_whole_block():
    """A controller running three loads out of four, on a machine nobody can
    reach, is worse than one that refuses to start and says why."""
    node = FakeNode({"cameras": CAMERAS,
                     "broken": dict(CAMERAS, polarity="sideways")})
    with pytest.raises(spec.SpecError):
        spec.load_control(node)
