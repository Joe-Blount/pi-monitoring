"""The words the override page shows for each load's mode and plan."""

import datetime
import os

import pytest

from monitoring import config
from monitoring import drivers as driver_registry
from monitoring.control import decide, irrigation, modes, spec

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def loads(*parts):
    node = config.load(os.path.join(ROOT, "sites", *parts),
                       known_drivers=driver_registry.names())
    return spec.load_control(node)


@pytest.fixture(scope="module")
def downstairs():
    return loads("blind1", "downstairs.yaml")


@pytest.fixture(scope="module")
def example():
    """The example's heater and irrigation loads, read one by one.

    The example file shows every key, including some no type accepts, so it
    is not read as a whole.
    """
    node = config.load(os.path.join(ROOT, "sites", "example", "node.yaml"),
                       known_drivers=driver_registry.names())
    devices = {device.name for device in node.devices}
    raw = node.control["loads"]
    return {name: spec.load_spec(name, dict(raw[name], enabled=True), devices)
            for name in raw
            if raw[name].get("type") in ("battery_heating", "sequenced_ac")}


def readings(age=5.0, **values):
    out = {}
    for key, value in values.items():
        device, field = key.split("__")
        out.setdefault(device, {"fields": {}, "age_seconds": age})[
            "fields"][field] = value
    return out


def source_readings(sources, values, age=5.0):
    out = {}
    for source, value in zip(sources, values):
        out.setdefault(source["device"], {"fields": {}, "age_seconds": age})[
            "fields"][source["field"]] = value
    return out


def ladder_mode(load, shedding=True, battery=42.0, **published):
    return modes.describe(load, load["name"], {load["name"]: published},
                          readings(bms__state_of_charge_pct=battery),
                          shedding)


def test_a_ladder_load_above_every_step_is_always_on(downstairs):
    lines = ladder_mode(downstairs["cameras_near"], shed_level=0.0,
                        reason_code=0.0)
    assert lines.mode == "Always on"
    assert lines.plan == ("Battery 42%. Below 50%: day only. Below 25%: dawn "
                          "and dusk. Below 10%: off.")
    assert lines.why is None


def test_each_ladder_step_names_its_schedule_and_the_hours_above_it(
        downstairs):
    load = downstairs["cameras_near"]
    assert ladder_mode(load, shed_level=1.0).mode == (
        "Day only (on 05:00 to 22:00, off at night)")
    assert ladder_mode(load, shed_level=2.0).mode == (
        "Dawn and dusk only (on from 1 h before sunrise to 2 h after "
        "sunrise, and from 2 h before sunset to 1 h after sunset), and only "
        "while 05:00 to 22:00")
    assert ladder_mode(load, shed_level=3.0).mode == "Off to save battery"


def test_steps_whose_hours_never_meet_are_both_named():
    day = {"type": "clock", "on_from": 5 * 60, "on_to": 22 * 60}
    night = {"type": "clock", "on_from": 22 * 60, "on_to": 5 * 60}
    mode = modes._latched_mode([{"schedule": day}, {"schedule": night}])
    assert mode == ("Night only (on 22:00 to 05:00, off by day), and only "
                    "while 05:00 to 22:00")


def test_the_floor_wins_over_the_step_and_the_shedding_switch(downstairs):
    lines = ladder_mode(downstairs["nvr"], shedding=False, shed_level=1.0,
                        reason_code=float(decide.FLOOR))
    assert lines.mode == "Off: battery at its floor"
    assert lines.plan == "Battery 42%. Below 5%: off."


def test_a_load_held_by_its_minimum_time_says_so_and_why(downstairs):
    lines = ladder_mode(
        downstairs["cameras_near"], shed_level=0.0, powered=0.0,
        reason_code=float(decide.HELD_BY_TIMER),
        reason="above every threshold; held for another 900s by the minimum "
               "off time")
    assert lines.mode == "Always on. Held off for now"
    assert "minimum off time" in lines.why


def test_shedding_switched_off_reads_as_always_on(downstairs):
    lines = ladder_mode(downstairs["cameras_far"], shedding=False,
                        shed_level=2.0)
    assert lines.mode == "Always on: shedding paused"


def test_a_step_held_on_by_an_untrusted_clock_says_so(downstairs):
    lines = ladder_mode(downstairs["cameras_far"], shed_level=1.0,
                        reason_code=float(decide.SCHEDULE_SUSPENDED))
    assert lines.mode.endswith("Held on: the clock is not trusted")


def test_a_ladder_load_with_nothing_published_says_so(downstairs):
    lines = modes.describe(downstairs["nvr"], "nvr", {}, {}, True)
    assert lines.mode == "Mode unknown: nothing published yet"
    assert lines.plan == "Battery no reading. Below 5%: off."


@pytest.mark.parametrize("level", [float("nan"), float("inf"), "2", True])
def test_a_damaged_shed_level_reads_as_unknown(downstairs, level):
    assert ladder_mode(downstairs["nvr"], shed_level=level).mode == (
        "Mode unknown: nothing published yet")


def test_a_reading_the_controller_counts_as_stale_is_not_shown(downstairs):
    load = downstairs["cameras_near"]
    old = load["shed_ladder"]["stale_after"] + 1
    lines = modes.describe(load, load["name"], {},
                           readings(age=old, bms__state_of_charge_pct=42.0),
                           True)
    assert lines.plan.startswith("Battery no reading.")
    lines = modes.describe(load, load["name"], {},
                           readings(age=float("inf"),
                                    bms__state_of_charge_pct=42.0), True)
    assert lines.plan.startswith("Battery no reading.")


def test_clock_and_solar_windows_are_named_by_their_shape():
    night = {"type": "clock", "on_from": 22 * 60, "on_to": 5 * 60}
    odd = {"type": "clock", "on_from": 0, "on_to": 13 * 60}
    zero = datetime.timedelta(0)
    daylight = {"type": "solar", "on_windows": [
        {"from": ("sunrise", zero), "to": ("sunset", zero)}]}
    assert modes.schedule_mode(night) == (
        "Night only (on 22:00 to 05:00, off by day)")
    assert modes.schedule_mode(odd) == "Set hours only (on 00:00 to 13:00)"
    assert modes.schedule_mode(daylight) == (
        "Daylight only (on from sunrise to sunset)")


def test_the_cpu_fan_shows_its_temperature_and_thresholds(downstairs):
    load = downstairs["cpu_fan"]
    lines = modes.describe(load, "cpu_fan", {"cpu_fan": {"powered": 1.0}},
                           readings(host__cpu_temp_f=168.4), True)
    assert lines.mode == "By temperature, running, 168 F"
    assert lines.plan == "Runs at 165 F or above, stops at 145 F or below."
    lines = modes.describe(load, "cpu_fan", {"cpu_fan": {"powered": 0.0}},
                           {}, True)
    assert lines.mode == "By temperature, idle, no reading"


def test_a_forced_fan_does_not_read_the_force_as_its_own_state(downstairs):
    load = downstairs["cpu_fan"]
    lines = modes.describe(
        load, "cpu_fan",
        {"cpu_fan": {"powered": 1.0, "reason_code": float(decide.OVERRIDE),
                     "reason": "forced on by an operator"}},
        readings(host__cpu_temp_f=120.0), True, forced=True)
    assert lines.mode == "By temperature, 120 F"
    assert lines.why is None


def test_the_vent_fan_shows_both_temperatures():
    load = loads("blind1", "upstairs.yaml")["vent_fan"]
    values = source_readings([load["inside"], load["outside"]], [91.0, 70.0])
    lines = modes.describe(load, "vent_fan", {"vent_fan": {"powered": 1.0}},
                           values, True)
    assert lines.mode == "By temperature, running, inside 91 F, outside 70 F"
    assert lines.plan.startswith("Runs when inside is at %g F or above"
                                 % load["inside_above_f"])


def test_the_heater_shows_its_coldest_sensor_and_its_reason(example):
    load = example["battery_heating"]
    values = source_readings(load["sources"],
                             [30.0 + i for i in range(len(load["sources"]))])
    lines = modes.describe(
        load, load["name"],
        {load["name"]: {"powered": 0.0, "reason": "no production"}}, values,
        True)
    assert lines.mode == "By temperature, idle, coldest sensor 30 F"
    assert lines.plan.startswith("Heats at %g F or below, stops at %g F or above" % (
        load["coldest_below_f"], load["coldest_above_f"]))
    assert lines.why == "no production"
    forced = modes.describe(load, load["name"],
                            {load["name"]: {"powered": 1.0}}, values, True,
                            forced=True)
    assert forced.mode == "By temperature, coldest sensor 30 F"


def run_mode(load, phase=None, inverter=0.0, forced=False, **published):
    if phase is not None:
        published["phase"] = irrigation.PHASE_CODES[phase]
    return modes.describe(
        load, load["name"],
        {load["name"]: published,
         irrigation.inverter_name(load["name"]): {"powered": inverter}},
        {}, True, forced=forced)


def test_each_irrigation_phase_has_its_words(example):
    load = example["irrigation"]
    assert run_mode(load, irrigation.IDLE).mode == (
        "Idle: waiting for the next scheduled start")
    assert run_mode(load, irrigation.STARTING).mode == "Starting"
    assert run_mode(load, irrigation.RUNNING).mode == "Running"
    assert run_mode(load, irrigation.STOPPING).mode == (
        "Stopping: inverter stays on to settle")
    assert run_mode(load, irrigation.IDLE, inverter=1.0).mode == (
        "Inverter only")
    assert run_mode(load, irrigation.IDLE, locked_out=1.0).mode == (
        "Locked out: too many failed starts")
    assert run_mode(load).mode == "Mode unknown: nothing published yet"
    assert run_mode(load).plan.startswith("Runs every 2 days.")


def test_an_irrigation_reason_stays_shown_after_its_pass(example):
    lines = run_mode(example["irrigation"], irrigation.IDLE,
                     reason_code=float(decide.NONE),
                     reason="skipped: the battery is too low")
    assert lines.why == "skipped: the battery is too low"


def test_a_manual_run_goes_back_to_idle(example):
    lines = run_mode(example["irrigation"], irrigation.RUNNING, inverter=1.0,
                     forced=True)
    assert lines.mode == "Idle: waiting for the next scheduled start"


def test_irrigation_with_no_schedule_is_manual_only(example):
    load = dict(example["irrigation"], schedule=None)
    lines = run_mode(load, irrigation.IDLE)
    assert lines.mode == "Idle: manual only"
    assert lines.plan.startswith("Runs only when started here.")


def test_the_inverter_says_whether_it_is_on_for_a_run_or_alone(example):
    load = example["irrigation"]
    name = irrigation.inverter_name(load["name"])

    def inverter(powered, phase, forced=False, reason=None):
        return modes.describe(
            load, name,
            {name: {"powered": powered, "reason": reason},
             load["name"]: {"phase": irrigation.PHASE_CODES[phase]}},
            {}, True, forced=forced)

    assert inverter(1.0, irrigation.RUNNING).mode == "On for irrigation"
    assert inverter(1.0, irrigation.IDLE).mode == "On alone for AC power"
    held = inverter(0.0, irrigation.IDLE, reason="held off by the battery")
    assert held.mode == "Off"
    assert held.why == "held off by the battery"
    assert inverter(1.0, irrigation.IDLE, forced=True).mode == (
        "Off unless irrigation runs")


def test_an_entry_that_is_not_a_mapping_reads_as_unknown(example):
    load = example["irrigation"]
    lines = modes.describe(load, load["name"],
                           {load["name"]: [],
                            irrigation.inverter_name(load["name"]): "x"},
                           {}, True)
    assert lines.mode == "Mode unknown: nothing published yet"


def test_the_floor_holds_against_a_force(downstairs):
    lines = ladder_mode(downstairs["nvr"], shed_level=0.0, floor_latched=1.0,
                        reason_code=float(decide.FLOOR))
    assert lines.mode == "Off: battery at its floor"
    assert lines.beats_force
    assert not ladder_mode(downstairs["nvr"], shed_level=0.0).beats_force


def test_a_force_the_controller_refuses_shows_why(example):
    lines = run_mode(example["irrigation"], irrigation.IDLE, forced=True,
                     reason_code=float(decide.RUN_REFUSED),
                     reason="a manual run cannot start: state of charge 40%")
    assert lines.why == "a manual run cannot start: state of charge 40%"
    downstairs = loads("blind1", "downstairs.yaml")
    plain = modes.describe(
        downstairs["cpu_fan"], "cpu_fan",
        {"cpu_fan": {"powered": 1.0, "reason": "forced on by an operator"}},
        {}, True, forced=True)
    assert plain.why is None


def test_a_reason_that_repeats_the_mode_is_not_shown(example):
    load = example["irrigation"]
    assert run_mode(load, irrigation.IDLE, reason="idle").why is None
    name = irrigation.inverter_name(load["name"])
    lines = modes.describe(load, name, {name: {"powered": 0.0,
                                               "reason": "off"}}, {}, True)
    assert lines.mode == "Off" and lines.why is None
