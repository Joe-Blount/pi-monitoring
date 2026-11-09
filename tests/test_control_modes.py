"""The words the override page shows for each load's mode and plan."""

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


def readings(**values):
    out = {}
    for key, value in values.items():
        device, field = key.split("__")
        out.setdefault(device, {"fields": {}})["fields"][field] = value
    return out


def ladder_mode(load, shedding=True, battery=42.0, **published):
    return modes.describe(load, load["name"], {load["name"]: published},
                          readings(bms__state_of_charge_pct=battery),
                          shedding)


def test_a_ladder_load_above_every_step_is_always_on(downstairs):
    mode, plan = ladder_mode(downstairs["cameras_near"], shed_level=0.0,
                             reason_code=0.0)
    assert mode == "Always on"
    assert plan == ("Battery 42%. Below 50%: day only. Below 25%: dawn and "
                    "dusk. Below 10%: off.")


def test_each_ladder_step_names_its_schedule(downstairs):
    load = downstairs["cameras_near"]
    assert ladder_mode(load, shed_level=1.0)[0] == (
        "Day only (on 05:00 to 22:00, off at night)")
    assert ladder_mode(load, shed_level=2.0)[0] == (
        "Dawn and dusk only (on from 1 h before sunrise to 2 h after "
        "sunrise, and from 2 h before sunset to 1 h after sunset)")
    assert ladder_mode(load, shed_level=3.0)[0] == "Off to save battery"


def test_the_floor_wins_over_the_step_and_the_shedding_switch(downstairs):
    load = downstairs["nvr"]
    mode, plan = ladder_mode(load, shedding=False, shed_level=1.0,
                             reason_code=float(decide.FLOOR))
    assert mode == "Off: battery at its floor"
    assert plan == "Battery 42%. Below 5%: off."


def test_shedding_switched_off_reads_as_always_on(downstairs):
    mode, _ = ladder_mode(downstairs["cameras_far"], shedding=False,
                          shed_level=2.0)
    assert mode == "Always on: shedding paused"


def test_a_step_held_on_by_an_untrusted_clock_says_so(downstairs):
    mode, _ = ladder_mode(downstairs["cameras_far"], shed_level=1.0,
                          reason_code=float(decide.SCHEDULE_SUSPENDED))
    assert mode.endswith("Held on: the clock is not trusted")


def test_a_ladder_load_with_nothing_published_says_so(downstairs):
    mode, plan = modes.describe(downstairs["nvr"], "nvr", {}, {}, True)
    assert mode == "Mode unknown: nothing published yet"
    assert plan == "Battery no reading. Below 5%: off."


def test_a_night_window_and_an_odd_window_read_as_such():
    night = {"type": "clock", "on_from": 22 * 60, "on_to": 5 * 60}
    odd = {"type": "clock", "on_from": 0, "on_to": 13 * 60}
    assert modes.schedule_mode(night) == (
        "Night only (on 22:00 to 05:00, off by day)")
    assert modes.schedule_mode(odd) == "Set hours only (on 00:00 to 13:00)"


def test_the_cpu_fan_shows_its_temperature_and_thresholds(downstairs):
    load = downstairs["cpu_fan"]
    mode, plan = modes.describe(load, "cpu_fan",
                                {"cpu_fan": {"powered": 1.0}},
                                readings(host__cpu_temp_f=168.4), True)
    assert mode == "By temperature, running, 168 F"
    assert plan == "Runs above 165 F, stops below 145 F."
    mode, _ = modes.describe(load, "cpu_fan", {"cpu_fan": {"powered": 0.0}},
                             {}, True)
    assert mode == "By temperature, idle, no reading"


def test_the_vent_fan_shows_both_temperatures():
    load = loads("blind1", "upstairs.yaml")["vent_fan"]
    inside, outside = load["inside"], load["outside"]
    values = {inside["device"]: {"fields": {inside["field"]: 91.0}},
              outside["device"]: {"fields": {outside["field"]: 70.0}}}
    mode, plan = modes.describe(load, "vent_fan",
                                {"vent_fan": {"powered": 1.0}}, values, True)
    assert mode == "By temperature, running, inside 91 F, outside 70 F"
    assert plan.startswith("Runs when inside is above %g F"
                           % load["inside_above_f"])


def test_the_heater_shows_its_coldest_sensor(example):
    load = example["battery_heating"]
    values = {}
    for index, source in enumerate(load["sources"]):
        values.setdefault(source["device"], {"fields": {}})["fields"][
            source["field"]] = 30.0 + index
    mode, plan = modes.describe(load, load["name"],
                                {load["name"]: {"powered": 1.0}}, values,
                                True)
    assert mode == "By temperature, heating, coldest 30 F"
    assert plan == "Heats below %g F, stops above %g F." % (
        load["coldest_below_f"], load["coldest_above_f"])


def run_mode(load, phase=None, inverter=0.0, **published):
    if phase is not None:
        published["phase"] = irrigation.PHASE_CODES[phase]
    return modes.describe(
        load, load["name"],
        {load["name"]: published,
         irrigation.inverter_name(load["name"]): {"powered": inverter}},
        {}, True)


def test_each_irrigation_phase_has_its_words(example):
    load = example["irrigation"]
    assert run_mode(load, irrigation.IDLE)[0] == (
        "Idle: waiting for the next scheduled day")
    assert run_mode(load, irrigation.STARTING)[0] == "Starting"
    assert run_mode(load, irrigation.RUNNING)[0] == "Running"
    assert run_mode(load, irrigation.STOPPING)[0] == (
        "Stopping: inverter stays on to settle")
    assert run_mode(load, irrigation.IDLE, inverter=1.0)[0] == "Inverter only"
    assert run_mode(load, irrigation.IDLE, locked_out=1.0)[0] == (
        "Locked out: too many dry starts")
    assert run_mode(load)[0] == "Mode unknown: nothing published yet"
    assert run_mode(load)[1].startswith("Runs every 2 days.")


def test_irrigation_with_no_schedule_is_manual_only(example):
    load = dict(example["irrigation"], schedule=None)
    mode, plan = run_mode(load, irrigation.IDLE)
    assert mode == "Idle: manual only"
    assert plan.startswith("Runs only when started here.")


def test_the_inverter_says_whether_it_is_on_for_a_run_or_alone(example):
    load = example["irrigation"]
    name = irrigation.inverter_name(load["name"])

    def inverter_mode(powered, phase):
        return modes.describe(
            load, name,
            {name: {"powered": powered},
             load["name"]: {"phase": irrigation.PHASE_CODES[phase]}},
            {}, True)[0]

    assert inverter_mode(1.0, irrigation.RUNNING) == "On for irrigation"
    assert inverter_mode(1.0, irrigation.IDLE) == "On alone for AC power"
    assert inverter_mode(0.0, irrigation.IDLE) == "Off"
