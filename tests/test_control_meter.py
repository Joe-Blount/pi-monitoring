"""The load meter: a load's draw is the step in the bank's current when the
controller switches it, kept only when nothing else could have moved it."""

import datetime

import pytest

from monitoring.control import meter

SPEC = {
    "shunt": {"device": "shunt", "field": "shunt_amps"},
    "charge": [{"device": "mppt", "field": "battery_amps"}],
    "wired_around": ["nvr"],
    "state_file": "/unused",
}

LOADS = {
    "cameras_far": {"type": "dc_branch"},
    "nvr": {"type": "dc_branch"},
    "cpu_fan": {"type": "cpu_fan"},
    "irrigation": {"type": "sequenced_ac",
                   "pump": {"expect_amps": (30.0, 60.0)}},
}

TZ = datetime.timezone(datetime.timedelta(hours=-6))


def _day(hour, minute=0):
    return datetime.datetime(2026, 11, 14, hour, minute, tzinfo=TZ)


SUNS = [{"sunrise": _day(6, 30) + datetime.timedelta(days=d),
         "sunset": _day(17, 15) + datetime.timedelta(days=d)}
        for d in (-1, 0, 1)]

NIGHT = _day(20, 40)
DAY = _day(12)
TWILIGHT = _day(17, 30)

OFF = {"cameras_far": False, "nvr": True, "cpu_fan": False,
       "irrigation.inverter": False, "irrigation": False}


class Bench:
    """One meter, stepped pass by pass on a ten-second loop with a shunt
    reading every sixty seconds."""

    def __init__(self, local, results=None, amps=-8.0, charge=0.0,
                 charge_age=5.0, lag=1.0, undriven=()):
        self.meter = meter.Meter(SPEC, LOADS, results)
        self.mono = 1000.0
        self.local = local
        self.amps = amps
        self.charge = charge
        self.charge_age = charge_age
        self.lag = lag
        self.taken = None
        self.connected = dict(OFF, cameras_far=True)
        for output in undriven:
            del self.connected[output]
        self.pass_()

    def readings(self):
        if self.taken is None or self.mono - self.taken >= 60.0:
            self.taken = self.mono - self.lag
        return {
            "shunt": {"fields": {"shunt_amps": self.amps},
                      "age_seconds": self.mono - self.taken,
                      "taken_at": self.taken},
            "mppt": {"fields": {"battery_amps": self.charge},
                     "age_seconds": self.charge_age,
                     "taken_at": self.mono - self.charge_age},
        }

    def pass_(self, trusted=True):
        now = self.local + datetime.timedelta(seconds=self.mono - 1000.0)
        self.meter.observe(self.mono, now.timestamp() if trusted else None,
                           now, SUNS, self.readings(), dict(self.connected))
        self.mono += 10.0

    def run(self, seconds, **kwargs):
        for _ in range(int(seconds // 10)):
            self.pass_(**kwargs)

    def switch(self, **states):
        self.connected.update(states)
        self.pass_()


def test_a_camera_branch_is_the_step_in_the_banks_current_at_night():
    bench = Bench(NIGHT)
    bench.run(120)
    bench.switch(cameras_far=False)
    bench.amps = -5.2
    bench.run(300)
    assert bench.meter.results["cameras_far"]["night"]["amps"] == pytest.approx(2.8)
    assert bench.meter.changed
    assert bench.meter.fields_for("cameras_far") == {
        "draw_night_amps": pytest.approx(2.8),
        "draw_night_at": pytest.approx(bench.meter.results["cameras_far"]["night"]["at"])}


def test_a_camera_branch_switched_on_by_day_is_kept_apart_from_night():
    bench = Bench(DAY)
    bench.connected["cameras_far"] = False
    bench.amps = -2.0
    bench.run(120)
    bench.switch(cameras_far=True)
    bench.amps = -4.0
    bench.run(300)
    assert set(bench.meter.results["cameras_far"]) == {"day"}
    assert bench.meter.results["cameras_far"]["day"]["amps"] == pytest.approx(2.0)


def test_a_camera_branch_switched_in_twilight_is_not_measured():
    bench = Bench(TWILIGHT)
    bench.run(120)
    bench.switch(cameras_far=False)
    bench.amps = -5.2
    bench.run(300)
    assert "cameras_far" not in bench.meter.results


def test_a_cameras_start_is_not_counted_in_its_draw():
    bench = Bench(DAY)
    bench.connected["cameras_far"] = False
    bench.amps = -2.0
    bench.run(120)
    bench.switch(cameras_far=True)
    bench.amps = -9.0
    bench.run(100)
    bench.amps = -4.0
    bench.run(200)
    assert bench.meter.results["cameras_far"]["day"]["amps"] == pytest.approx(2.0)


def test_charge_coming_in_while_a_step_is_measured_drops_it():
    bench = Bench(NIGHT)
    bench.run(120)
    bench.switch(cameras_far=False)
    bench.amps = -5.2
    bench.run(150)
    bench.charge = 1.0
    bench.run(150)
    assert bench.meter.results == {}


def test_a_switch_while_charge_comes_in_is_not_measured():
    bench = Bench(NIGHT, charge=4.0, amps=-1.0)
    bench.run(120)
    bench.switch(cameras_far=False)
    bench.charge = 0.0
    bench.run(300)
    assert bench.meter.results == {}


def test_another_switch_while_a_step_is_measured_drops_it():
    bench = Bench(NIGHT)
    bench.run(120)
    bench.switch(cameras_far=False)
    bench.amps = -5.2
    bench.run(60)
    bench.switch(cpu_fan=True)
    bench.run(300)
    assert "cameras_far" not in bench.meter.results


def test_two_relays_switched_in_one_pass_are_not_measured():
    bench = Bench(NIGHT)
    bench.run(120)
    bench.switch(cameras_far=False, cpu_fan=True)
    bench.amps = -5.0
    bench.run(300)
    assert bench.meter.results == {}


def test_a_switch_with_no_reading_since_the_switch_before_is_not_measured():
    bench = Bench(NIGHT)
    bench.run(120)
    bench.switch(cpu_fan=True)
    bench.switch(cameras_far=False)
    bench.amps = -5.0
    bench.run(300)
    assert "cameras_far" not in bench.meter.results


def test_nothing_is_measured_without_a_trusted_clock():
    bench = Bench(NIGHT)
    bench.run(120)
    bench.connected["cameras_far"] = False
    bench.pass_(trusted=False)
    bench.amps = -5.2
    bench.run(300)
    assert bench.meter.results == {}


def test_a_load_wired_around_its_relay_is_not_measured():
    bench = Bench(NIGHT)
    bench.run(120)
    bench.switch(nvr=False)
    bench.amps = -7.96
    bench.run(300)
    assert bench.meter.results == {}


@pytest.mark.parametrize("pump_amps, condition", [
    (46.4, "wet"), (17.4, "dry"), (75.0, "high")])
def test_the_pump_is_measured_with_its_inverter_from_the_reading_before_it(
        pump_amps, condition):
    bench = Bench(DAY, amps=-2.0)
    bench.connected["cameras_far"] = False
    bench.run(120)
    bench.switch(**{"irrigation.inverter": True})
    bench.amps = -5.0
    bench.run(10)
    bench.switch(irrigation=True)
    bench.amps = -2.0 - pump_amps
    bench.run(150)
    figures = bench.meter.results["irrigation"]
    assert set(figures) == {condition}
    assert figures[condition]["amps"] == pytest.approx(pump_amps)
    assert "irrigation.inverter" not in bench.meter.results


def test_the_inverter_alone_is_not_measured():
    """A force may run it to power a tool, whose draw would be its own."""
    bench = Bench(DAY, amps=-2.0)
    bench.connected["cameras_far"] = False
    bench.run(120)
    bench.switch(**{"irrigation.inverter": True})
    bench.amps = -5.0
    bench.run(300)
    bench.switch(**{"irrigation.inverter": False})
    bench.amps = -2.0
    bench.run(300)
    assert bench.meter.results == {}


def test_the_pumps_stop_is_not_measured():
    """The inverter may stay on after it, so the step is not the pump's
    start reversed."""
    bench = Bench(DAY, amps=-48.4)
    bench.connected.update(cameras_far=False, irrigation=True,
                           **{"irrigation.inverter": True})
    bench.run(120)
    bench.switch(irrigation=False)
    bench.amps = -5.0
    bench.run(300)
    assert bench.meter.results == {}


def test_a_reading_stamped_soon_after_a_switch_does_not_start_the_next_step():
    """It may have been taken before the switch, and hold the load still
    running."""
    bench = Bench(NIGHT, lag=1.0)
    bench.run(100)
    bench.switch(cpu_fan=True)
    bench.run(10)
    bench.switch(cameras_far=False)
    bench.amps = -5.0
    bench.run(300)
    assert "cameras_far" not in bench.meter.results


def test_a_reading_stamped_well_after_a_switch_starts_the_next_step():
    bench = Bench(NIGHT, lag=1.0)
    bench.run(60)
    bench.switch(cpu_fan=True)
    bench.run(60)
    bench.switch(cameras_far=False)
    bench.amps = -5.0
    bench.run(300)
    assert "cameras_far" in bench.meter.results


def test_a_charge_source_gone_quiet_counts_as_charge_coming_in():
    """The panels may be carrying the load, hiding the step."""
    bench = Bench(DAY, charge=6.0, charge_age=600.0, amps=-0.1)
    bench.run(120)
    bench.switch(cameras_far=False)
    bench.amps = -0.05
    bench.run(300)
    bench.run(1200)
    assert bench.meter.results == {}


def test_the_base_load_is_read_after_every_switched_load_is_off_ten_minutes():
    bench = Bench(NIGHT, amps=-1.8)
    bench.switch(cameras_far=False)
    bench.run(600)
    assert "base" not in bench.meter.results
    bench.run(600)
    assert bench.meter.results["base"]["amps"] == pytest.approx(1.8)
    assert bench.meter.base_fields() == {
        "base_amps": pytest.approx(1.8),
        "base_at": pytest.approx(bench.meter.results["base"]["at"])}


def test_the_base_load_is_written_again_only_when_it_moves():
    bench = Bench(NIGHT, amps=-1.8)
    bench.switch(cameras_far=False)
    bench.run(1200)
    assert bench.meter.changed
    bench.meter.changed = False
    bench.amps = -1.82
    bench.run(1200)
    assert not bench.meter.changed
    bench.amps = -1.9
    bench.run(1200)
    assert bench.meter.changed


def test_the_base_load_is_not_read_while_a_switched_load_runs():
    bench = Bench(NIGHT, amps=-8.0)
    bench.run(1800)
    assert "base" not in bench.meter.results


def test_figures_kept_on_the_card_come_back_and_malformed_ones_do_not():
    kept = {
        "cameras_far": {"night": {"amps": 2.9, "at": 1.0e9},
                        "day": {"amps": "two", "at": 1.0e9}},
        "cpu_fan": {"": {"amps": 0.17, "at": True}},
        "base": {"amps": 1.8, "at": 1.0e9},
        "junk": ["not", "figures"],
        "irrigation.inverter": {"idle": {"amps": 3.0, "at": 1.0e9}},
        "disabled": {"wet": {"amps": 46.4, "at": 1.0e9}},
    }
    bench = Bench(NIGHT, results=kept)
    assert bench.meter.results == {
        "cameras_far": {"night": {"amps": 2.9, "at": 1.0e9}},
        "disabled": {"wet": {"amps": 46.4, "at": 1.0e9}},
        "base": {"amps": 1.8, "at": 1.0e9},
    }
    assert bench.meter.fields_for("cameras_far") == {
        "draw_night_amps": 2.9, "draw_night_at": 1.0e9}
    assert bench.meter.fields_for("cpu_fan") == {}
    assert not bench.meter.changed


def test_a_newer_figure_replaces_the_one_kept_on_the_card():
    bench = Bench(NIGHT, results={
        "cameras_far": {"night": {"amps": 2.0, "at": 1.0e9}}})
    bench.run(120)
    bench.switch(cameras_far=False)
    bench.amps = -5.0
    bench.run(300)
    assert bench.meter.results["cameras_far"]["night"]["amps"] == \
        pytest.approx(3.0)


@pytest.mark.parametrize("local, expected", [
    (_day(5, 59), "night"), (_day(6, 1), None), (_day(7, 31), "day"),
    (_day(16, 14), "day"), (_day(16, 16), None), (_day(17, 46), "night"),
])
def test_the_period_of_a_switch_by_the_sun(local, expected):
    assert meter.period(local, SUNS) == expected


def test_a_switch_without_a_sun_has_no_period():
    assert meter.period(NIGHT, []) is None


def test_a_pump_started_long_after_its_inverter_is_not_measured():
    """The inverter may have been running a tool meanwhile."""
    bench = Bench(DAY, amps=-2.0)
    bench.connected["cameras_far"] = False
    bench.run(120)
    bench.switch(**{"irrigation.inverter": True})
    bench.amps = -5.0
    bench.run(60)
    bench.switch(irrigation=True)
    bench.amps = -48.4
    bench.run(150)
    assert bench.meter.results == {}


def test_the_pumps_figure_is_not_taken_from_a_reading_stamped_soon_after_its_start():
    """It may have been taken before the pump ran."""
    bench = Bench(DAY, amps=-2.0, lag=1.0)
    bench.connected["cameras_far"] = False
    bench.run(80)
    bench.switch(**{"irrigation.inverter": True})
    bench.amps = -5.0
    bench.switch(irrigation=True)
    bench.run(20)
    bench.amps = -48.4
    bench.run(150)
    figures = bench.meter.results["irrigation"]
    assert figures["wet"]["amps"] == pytest.approx(46.4)


def test_a_switch_soon_after_charge_stops_is_not_measured():
    """The reading the step would start from may still hold the charge."""
    bench = Bench(NIGHT, charge=3.0, amps=-8.0)
    bench.run(120)
    bench.charge = 0.0
    bench.run(10)
    bench.switch(cameras_far=False)
    bench.amps = -3.0
    bench.run(300)
    assert bench.meter.results == {}


def test_a_relays_first_drive_drops_a_step_being_measured():
    bench = Bench(NIGHT, undriven=["cpu_fan"])
    bench.run(120)
    bench.switch(cameras_far=False)
    bench.amps = -5.0
    bench.run(60)
    bench.switch(cpu_fan=True)
    bench.amps = -6.0
    bench.run(300)
    assert "cameras_far" not in bench.meter.results


def test_a_positive_shunt_reading_while_a_camera_settles_drops_the_step():
    bench = Bench(NIGHT)
    bench.run(120)
    bench.switch(cameras_far=False)
    bench.amps = 1.0
    bench.run(60)
    bench.amps = -3.0
    bench.run(300)
    assert bench.meter.results == {}


def test_the_base_load_is_not_read_while_an_undriven_load_runs():
    """A load the controller does not drive may sit at a fail-safe that
    runs it."""
    bench = Bench(NIGHT, amps=-4.8)
    bench.meter.spec = dict(SPEC, running_undriven=["cameras_near"])
    bench.switch(cameras_far=False)
    bench.run(1800)
    assert "base" not in bench.meter.results
