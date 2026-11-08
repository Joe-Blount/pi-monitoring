"""The irrigation load: an inverter and a pump behind it.

Each test walks the decision pass by pass, the way the loop calls it, and
holds the clocks and the readings still between passes.
"""

import copy
import datetime
import zoneinfo

import pytest
import yaml

from monitoring.control import decide, irrigation, spec

ZONE = zoneinfo.ZoneInfo("America/Chicago")
SUNRISE = datetime.datetime(2026, 6, 21, 6, 10, tzinfo=ZONE)
SUNSET = datetime.datetime(2026, 6, 21, 20, 53, tzinfo=ZONE)
DAY = datetime.timedelta(days=1)
SUNS = [{"sunrise": SUNRISE - DAY, "sunset": SUNSET - DAY},
        {"sunrise": SUNRISE, "sunset": SUNSET},
        {"sunrise": SUNRISE + DAY, "sunset": SUNSET + DAY}]

BLOCK = yaml.safe_load("""
type: sequenced_ac
enabled: true
on_stale_data: deny
inverter:
  control_pin: 25
  polarity: energize_to_connect
  settle_after_on: 10s
pump:
  control_pin: 22
  polarity: energize_to_connect
  settle_before_inverter_off: 3s
  readback: {source: load, pin: null, expect_amps: {min: 30, max: 60}}
sources:
  soc: {device: bms, field: state_of_charge_pct}
  volts: {device: mppt, field: battery_volts}
  shunt: {device: shunt, field: shunt_amps}
  charge_state: {device: mppt, field: charge_state_code}
stale_after: 10m
shunt_stale_after: 150s
schedule: null
start_conditions:
  charge_state_was_in: [float, absorption]
  within: 6h
  soc_above_pct: 40
  battery_volts_above: 13.0
continue_conditions:
  max_run: 45m
  soc_above_pct: 30
  battery_volts_above: 12.6
limits:
  cooldown: 2h
  max_amp_hours_per_day: 160
  failed_start_lockout: 3
dry_run_check:
  settle: 15s
  deadline: 150s
absolute_max_run: 60m
state_file: /var/lib/monitoring/irrigation.json
floor:
  sources:
    - {device: bms, field: cell_min_volts, shed_below: 3.15,
       restore_above: 3.25}
""")

DEVICES = {"bms", "mppt", "shunt"}


def parsed(**changes):
    raw = copy.deepcopy(BLOCK)
    raw.update(changes)
    return spec.load_spec("irrigation", raw, DEVICES)


class Site:
    """The readings, the clocks and the switches, moved by a test."""

    def __init__(self, spec_=None, start=None):
        self.spec = spec_ or parsed()
        self.now = start or datetime.datetime(2026, 6, 21, 12, 0, tzinfo=ZONE)
        self.mono = 1000.0
        self.soc = 80.0
        self.cell = 3.30
        self.volts = 13.4
        self.charge_state = 5.0
        self.amps = -2.0          # the battery discharge before the pump
        self.shunt_at = self.mono
        self.switches = {"shedding_enabled": True, "loads": {}}
        self.state = {}
        self.trusted = True
        self.saves = True
        self.history = []

    def readings(self):
        return {
            "bms": {"fields": {"state_of_charge_pct": self.soc,
                               "cell_min_volts": self.cell},
                    "age_seconds": 30.0, "taken_at": self.mono - 30},
            "mppt": {"fields": {"battery_volts": self.volts,
                                "charge_state_code": self.charge_state},
                     "age_seconds": 1.0, "taken_at": self.mono - 1},
            "shunt": {"fields": {"shunt_amps": self.amps},
                      "age_seconds": self.mono - self.shunt_at,
                      "taken_at": self.shunt_at},
        }

    def shunt(self, amps):
        """A new shunt reading, taken now."""
        self.amps = amps
        self.shunt_at = self.mono

    def force(self, name, state, minutes):
        self.switches["loads"][name] = {
            "force": state,
            "expires_epoch": self.now.timestamp() + minutes * 60}

    def step(self, seconds=10):
        self.now += datetime.timedelta(seconds=seconds)
        self.mono += seconds
        decisions, self.state = decide.decide(
            self.now, self.mono, self.trusted, self.readings(),
            self.switches, {"irrigation": self.spec}, self.state, SUNS)
        # The loop writes the record after each pass and says whether it
        # reached the card.
        self.state["irrigation"]["persist_saved"] = self.saves
        pump = decisions["irrigation"]
        inverter = decisions["irrigation.inverter"]
        assert not (pump.connected and not inverter.connected), \
            "the pump ran without the inverter"
        self.history.append((inverter.connected, pump.connected))
        return pump, inverter

    @property
    def persist(self):
        return self.state["irrigation"]["persist"]


def run_to_pump(site):
    """Ask for a run, unless one is asked for, and step until the pump is
    on."""
    if "irrigation" not in site.switches["loads"]:
        site.force("irrigation", "on", 20)
    for _ in range(10):
        pump, _ = site.step()
        if pump.connected:
            return
    raise AssertionError("the pump never started")


# -- the order ----------------------------------------------------------------

def test_a_run_records_itself_then_starts_the_inverter_then_the_pump():
    site = Site()
    site.force("irrigation", "on", 20)
    pump, inverter = site.step()
    assert not inverter.connected and not pump.connected
    assert "recording" in pump.reason
    pump, inverter = site.step()
    assert inverter.connected and not pump.connected
    pump, inverter = site.step(5)
    assert not pump.connected, "the pump started before the inverter settled"
    pump, inverter = site.step(5)
    assert inverter.connected and pump.connected


def test_a_run_whose_record_cannot_be_saved_does_not_start():
    site = Site()
    site.saves = False
    site.force("irrigation", "on", 20)
    for _ in range(3):
        pump, inverter = site.step()
        assert not inverter.connected
    assert "recorded on the card" in pump.reason


def test_a_stop_turns_the_pump_off_first_and_the_inverter_after():
    site = Site()
    run_to_pump(site)
    site.switches["loads"].pop("irrigation")
    pump, inverter = site.step()
    assert not pump.connected and inverter.connected
    pump, inverter = site.step()
    assert not pump.connected and not inverter.connected


def test_nothing_runs_without_a_request():
    site = Site()
    for _ in range(5):
        pump, inverter = site.step()
        assert not pump.connected and not inverter.connected


# -- the dry-run check ----------------------------------------------------------

def test_a_pump_drawing_its_range_keeps_running():
    site = Site()
    run_to_pump(site)
    site.step(15)
    site.shunt(-2.0 - 44.7)
    pump, _ = site.step()
    assert pump.connected
    assert "44.7 A" in pump.reason


@pytest.mark.parametrize("draw,words", [(16.1, "dry"), (75.0, "stalled")])
def test_a_pump_outside_its_range_stops_and_counts_a_failed_start(draw,
                                                                  words):
    site = Site()
    run_to_pump(site)
    site.step(15)
    site.shunt(-2.0 - draw)
    pump, inverter = site.step()
    assert not pump.connected and inverter.connected
    assert words in pump.reason
    assert site.persist["failed_today"] == 1


def test_a_reading_taken_before_the_settle_time_is_not_the_check():
    site = Site()
    run_to_pump(site)
    site.step(5)
    site.shunt(-2.0 - 3.0)      # the pump still spinning up
    pump, _ = site.step(1)
    assert pump.connected


def test_a_pump_the_shunt_never_sees_stops_and_counts_a_failed_start():
    site = Site()
    run_to_pump(site)
    for _ in range(30):
        pump, _ = site.step(10)
        if not pump.connected:
            break
    assert not pump.connected
    assert site.mono - site.shunt_at <= 160
    assert site.persist["failed_today"] == 1


def test_three_failed_starts_lock_out_until_the_next_day():
    site = Site()
    for _ in range(3):
        site.switches["loads"].pop("irrigation", None)
        site.step(60)
        run_to_pump(site)
        site.step(15)
        site.shunt(-2.0 - 10.0)
        site.step()
        site.step()
        site.step()
    assert site.persist["lockout_day"] == "2026-06-21"
    site.switches["loads"].pop("irrigation")
    site.step()
    site.force("irrigation", "on", 20)
    pump, inverter = site.step()
    assert not inverter.connected
    assert "locked out" in pump.reason
    site.now += datetime.timedelta(days=1)
    site.force("irrigation", "on", 20)
    site.step()
    _, inverter = site.step()
    assert inverter.connected


def test_clearing_a_lockout_lets_a_run_start():
    site = Site()
    site.state = {"irrigation": {"persist": {
        "day": "2026-06-21", "lockout_day": "2026-06-21", "failed_today": 3,
        "amp_hours_today": 0.0, "skipped_today": [], "consumed": []}}}
    site.switches["loads"]["irrigation.clear_lockout"] = {
        "at": site.now.timestamp()}
    site.force("irrigation", "on", 20)
    site.step()
    _, inverter = site.step()
    assert inverter.connected
    assert site.persist["failed_today"] == 0


# -- the stops while running ----------------------------------------------------

def checked_run(site):
    run_to_pump(site)
    site.step(15)
    site.shunt(-2.0 - 44.7)
    pump, _ = site.step()
    assert pump.connected


def test_the_battery_falling_below_the_continue_minimum_stops_the_run():
    site = Site()
    checked_run(site)
    site.soc = 29.0
    pump, _ = site.step()
    assert not pump.connected
    assert "29.0%" in pump.reason


def test_a_stale_shunt_stops_the_run():
    site = Site()
    checked_run(site)
    pump, _ = site.step(160)
    assert not pump.connected
    assert "shunt" in pump.reason


def test_the_run_limit_stops_a_run_asked_for_longer():
    site = Site()
    site.force("irrigation", "on", 120)
    checked_run(site)
    for _ in range(50):
        site.shunt(-2.0 - 44.7)
        pump, _ = site.step(60)
        if not pump.connected:
            break
    assert "run limit" in pump.reason
    assert site.persist["last_run_day"] == "2026-06-21"


def test_the_run_ends_when_its_request_expires():
    site = Site()
    site.force("irrigation", "on", 2)
    checked_run(site)
    for _ in range(5):
        site.shunt(-2.0 - 44.7)
        pump, _ = site.step(30)
        if not pump.connected:
            break
    assert not pump.connected


def test_one_request_runs_once():
    site = Site()
    site.force("irrigation", "on", 20)
    checked_run(site)
    site.soc = 29.0
    site.step()
    site.soc = 80.0
    for _ in range(5):
        pump, inverter = site.step()
    assert not pump.connected and not inverter.connected


def test_the_amp_hours_of_a_run_are_counted():
    site = Site()
    checked_run(site)
    for _ in range(6):
        site.shunt(-2.0 - 40.0)
        site.step(60)
    # Six minutes at forty amps, and the reading that passed the check.
    assert 3.5 < site.persist["amp_hours_today"] < 5.5


def test_the_daily_amp_hour_limit_refuses_a_start():
    site = Site()
    site.state = {"irrigation": {"persist": {
        "day": "2026-06-21", "amp_hours_today": 160.0, "failed_today": 0,
        "skipped_today": [], "consumed": []}}}
    site.force("irrigation", "on", 20)
    pump, inverter = site.step()
    assert not inverter.connected and "160 Ah" in pump.reason


def test_a_restart_during_a_run_ends_it():
    site = Site()
    checked_run(site)
    for held in site.state.values():
        if held.get("commanded"):
            held["restarted"] = True
    pump, inverter = site.step()
    assert not pump.connected and not inverter.connected
    assert "restarted" in pump.reason
    for _ in range(3):
        pump, inverter = site.step()
        assert not pump.connected and not inverter.connected


# -- start conditions -----------------------------------------------------------

def test_a_manual_run_needs_the_start_minimums():
    site = Site()
    site.soc = 39.0
    site.force("irrigation", "on", 20)
    pump, inverter = site.step()
    assert not inverter.connected
    assert "39.0%" in pump.reason


def test_a_manual_run_does_not_need_the_charge_state():
    site = Site()
    site.charge_state = 3.0     # bulk
    site.force("irrigation", "on", 20)
    site.step()
    _, inverter = site.step()
    assert inverter.connected


def test_nothing_runs_on_an_untrusted_clock():
    site = Site()
    site.trusted = False
    site.force("irrigation", "on", 20)
    _, inverter = site.step()
    assert not inverter.connected


def test_a_force_off_stops_a_run_and_blocks_the_schedule():
    site = Site()
    checked_run(site)
    site.force("irrigation", "off", 60)
    pump, _ = site.step()
    assert not pump.connected


# -- the schedule ---------------------------------------------------------------

SCHEDULE = {"every_days": 2, "starts": [{"at": "12:01", "run": "20m"}]}


def test_a_scheduled_start_runs_when_its_conditions_hold():
    site = Site(parsed(schedule=SCHEDULE))
    _, inverter = site.step(30)
    assert not inverter.connected       # 12:00:30
    site.step(60)
    _, inverter = site.step()
    assert inverter.connected           # 12:01:40


def test_a_scheduled_start_needs_the_charge_state():
    site = Site(parsed(schedule=SCHEDULE))
    site.charge_state = 3.0
    site.step(90)
    site.shunt(-2.0)
    pump, inverter = site.step()
    assert not inverter.connected
    assert "has not been in float or absorption" in pump.reason


def test_a_skipped_start_does_not_try_again_that_day():
    site = Site(parsed(schedule=SCHEDULE))
    site.charge_state = 3.0
    site.step(90)
    site.charge_state = 5.0
    _, inverter = site.step()
    assert not inverter.connected


def test_the_schedule_waits_every_days_after_a_run():
    site = Site(parsed(schedule=SCHEDULE))
    site.state = {"irrigation": {"persist": {
        "day": "2026-06-21", "last_run_day": "2026-06-20",
        "amp_hours_today": 0.0, "failed_today": 0, "skipped_today": [],
        "consumed": []}}}
    _, inverter = site.step(90)
    assert not inverter.connected


def test_a_solar_start_is_placed_from_the_days_sunrise():
    raw = {"every_days": 1, "starts": [{"at": ["sunrise", "6h"],
                                        "run": "20m"}]}
    site = Site(parsed(schedule=raw))
    site.step(60 * 11)                   # 12:11, sunrise 06:10 plus 6 h
    _, inverter = site.step()
    assert inverter.connected


# -- the inverter alone ---------------------------------------------------------

def test_the_inverter_can_run_alone_and_the_pump_stays_off():
    site = Site()
    site.force("irrigation.inverter", "on", 60)
    for _ in range(5):
        pump, inverter = site.step()
        assert inverter.connected and not pump.connected


def test_the_inverter_alone_stops_at_the_battery_floor_and_waits_to_return():
    site = Site()
    site.force("irrigation.inverter", "on", 60)
    site.step()
    site.soc = 29.5
    _, inverter = site.step()
    assert not inverter.connected
    site.soc = 35.0
    _, inverter = site.step()
    assert not inverter.connected, "it came back on below the start minimum"
    site.soc = 41.0
    _, inverter = site.step()
    assert inverter.connected


def test_the_inverter_held_on_alone_stays_on_after_a_run():
    site = Site()
    site.force("irrigation.inverter", "on", 120)
    checked_run(site)
    site.switches["loads"].pop("irrigation")
    for _ in range(3):
        pump, inverter = site.step()
    assert not pump.connected and inverter.connected


def test_the_inverter_forced_off_stops_a_run_pump_first():
    site = Site()
    checked_run(site)
    site.force("irrigation.inverter", "off", 60)
    pump, inverter = site.step()
    assert not pump.connected and inverter.connected
    pump, inverter = site.step()
    assert not pump.connected and not inverter.connected


def test_the_inverter_alone_stays_on_while_a_run_starts():
    site = Site()
    site.force("irrigation.inverter", "on", 120)
    site.step()
    site.force("irrigation", "on", 20)
    for _ in range(4):
        _, inverter = site.step()
        assert inverter.connected


def test_a_cell_under_the_floor_stops_a_run_and_the_inverter_alone():
    site = Site()
    site.force("irrigation.inverter", "on", 120)
    checked_run(site)
    site.cell = 3.10
    pump, inverter = site.step()
    assert not pump.connected and "floor" in pump.reason
    site.step()
    _, inverter = site.step()
    assert not inverter.connected


def test_a_cell_under_the_floor_refuses_a_start():
    site = Site()
    site.cell = 3.10
    site.force("irrigation", "on", 20)
    pump, inverter = site.step()
    assert not inverter.connected and "floor" in pump.reason


def test_a_run_waits_for_a_battery_reading_while_starting():
    site = Site()
    site.force("irrigation", "on", 20)
    site.step()
    site.step()
    site.soc = 29.0
    pump, inverter = site.step()
    assert not inverter.connected and not pump.connected


# -- the specification ----------------------------------------------------------

def test_the_shipped_shape_parses():
    out = parsed()
    assert out["pump"]["expect_amps"] == (30.0, 60.0)
    assert spec.outputs(out) == [("irrigation.inverter", 25,
                                  "energize_to_connect"),
                                 ("irrigation", 22, "energize_to_connect")]


@pytest.mark.parametrize("key", ["guard", "request_file"])
def test_a_key_nothing_reads_is_refused(key):
    with pytest.raises(spec.SpecError, match=key):
        parsed(**{key: {}})


def test_a_relay_that_a_dead_controller_would_leave_on_is_refused():
    inverter = dict(BLOCK["inverter"], polarity="energize_to_shed")
    with pytest.raises(spec.SpecError, match="energize_to_connect"):
        parsed(inverter=inverter)


def test_continue_minimums_above_the_start_minimums_are_refused():
    keep = dict(BLOCK["continue_conditions"], soc_above_pct=45)
    with pytest.raises(spec.SpecError, match="below the start"):
        parsed(continue_conditions=keep)


def test_a_disabled_load_still_has_its_keys_checked():
    raw = copy.deepcopy(BLOCK)
    raw.update(enabled=False, guard={})
    with pytest.raises(spec.SpecError, match="guard"):
        spec.load_spec("irrigation", raw, DEVICES)
    raw.pop("guard")
    assert spec.load_spec("irrigation", raw, DEVICES) is None


def test_the_inverter_name_is_the_load_name_with_a_suffix():
    assert irrigation.inverter_name("irrigation") == "irrigation.inverter"


# -- the loop -------------------------------------------------------------------

def irrigation_loop(tmp_path, site, raw=None):
    from monitoring.control import clock, loop, relay
    from tests.test_control_loop import BOOT, LOCATION, FakeNode

    block = copy.deepcopy(raw or BLOCK)
    block["state_file"] = str(tmp_path / "irrigation.json")
    pins = {"irrigation.inverter": relay.Pretend(25, "energize_to_connect",
                                                 relay.HIGH),
            "irrigation": relay.Pretend(22, "energize_to_connect",
                                        relay.HIGH)}
    published = {}
    machine = loop.Loop(
        FakeNode({"irrigation": block}, devices=("bms", "mppt", "shunt")),
        LOCATION, ZONE, relays=pins,
        now=lambda: site.now, monotonic=lambda: site.mono,
        trust=lambda high_water=None: clock.Trust(True, "under test"),
        readings=site.readings, switches=lambda: site.switches,
        state_path=str(tmp_path / "state.json"),
        published_path=str(tmp_path / "published.json"),
        high_water_path=str(tmp_path / "hw.json"),
        publish=lambda _path, data: published.update(data), boot=BOOT)
    return machine, pins, published


def test_the_loop_drives_both_relays_and_publishes_both(tmp_path):
    site = Site()
    machine, pins, published = irrigation_loop(tmp_path, site)
    site.force("irrigation.inverter", "on", 60)
    machine.once()
    assert pins["irrigation.inverter"].levels[-1:] == [True]
    assert pins["irrigation"].levels[-1:] in ([], [False])
    loads = published["loads"]
    assert loads["irrigation.inverter"]["powered"] == 1.0
    assert loads["irrigation"]["amp_hours_today"] == 0.0
    assert loads["irrigation"]["locked_out"] == 0.0


def test_the_days_record_comes_back_after_a_reboot(tmp_path):
    from monitoring.control import files
    site = Site()
    record = {"day": "2026-06-21", "lockout_day": "2026-06-21",
              "failed_today": 3, "amp_hours_today": 12.0,
              "skipped_today": [], "consumed": []}
    files.write_json(str(tmp_path / "irrigation.json"), record)
    machine, pins, published = irrigation_loop(tmp_path, site)
    site.force("irrigation", "on", 20)
    machine.once()
    assert True not in pins["irrigation.inverter"].levels
    assert published["loads"]["irrigation"]["locked_out"] == 1.0


def test_the_days_record_is_written_when_it_changes(tmp_path):
    from monitoring.control import files
    site = Site()
    machine, _, _ = irrigation_loop(tmp_path, site)
    site.force("irrigation", "on", 20)
    machine.once()
    saved = files.read_json(str(tmp_path / "irrigation.json"))
    assert saved["consumed"], "the request a run used was not recorded"


# -- the record across clocks and restarts ----------------------------------

def test_an_untrusted_clock_does_not_reset_the_days_counts():
    site = Site()
    site.state = {"irrigation": {"persist": {
        "day": "2026-06-21", "amp_hours_today": 160.0, "failed_today": 2,
        "skipped_today": [], "consumed": []}}}
    site.trusted = False
    site.now -= datetime.timedelta(days=1)
    site.step()
    assert site.persist["amp_hours_today"] == 160.0
    site.trusted = True
    site.now += datetime.timedelta(days=1)
    site.step()
    assert site.persist["amp_hours_today"] == 160.0


def test_a_clock_stepped_back_does_not_reset_the_days_counts():
    site = Site()
    site.state = {"irrigation": {"persist": {
        "day": "2026-06-22", "amp_hours_today": 160.0, "failed_today": 2,
        "skipped_today": [], "consumed": []}}}
    site.step()
    assert site.persist["amp_hours_today"] == 160.0


def test_a_run_a_reboot_ended_starts_the_cooldown():
    site = Site(parsed(schedule={"every_days": 1,
                                 "starts": [{"at": "12:00", "run": "20m"}]}))
    site.state = {"irrigation": {"persist": {
        "day": "2026-06-21", "amp_hours_today": 0.0, "failed_today": 0,
        "skipped_today": [], "consumed": [], "active_run": "manual:1.0"}}}
    pump, inverter = site.step()
    assert not inverter.connected
    assert "cooldown" in pump.reason
    assert "active_run" not in site.persist
    assert site.persist["last_run_end_epoch"] > 0


def test_the_sample_a_run_stops_on_is_counted():
    site = Site()
    site.force("irrigation", "on", 2)
    checked_run(site)
    before = site.persist["amp_hours_today"]
    site.step(60)
    site.shunt(-2.0 - 40.0)
    for _ in range(3):
        site.step(30)
    assert site.persist["amp_hours_today"] > before


def test_a_reading_after_the_deadline_is_not_the_check():
    site = Site()
    run_to_pump(site)
    pump_on = site.mono
    site.step(5)
    site.shunt(-2.0 - 3.0)                 # before the settle time
    site.amps = -2.0 - 44.7
    site.shunt_at = pump_on + 15 + 150 + 5  # past the deadline
    site.mono = site.shunt_at
    pump, _ = site.step(1)
    assert not pump.connected
    assert site.persist["failed_today"] == 1


def test_a_second_start_runs_on_a_watering_day():
    raw = {"every_days": 2, "starts": [{"at": "12:01", "run": "5m"},
                                       {"at": "14:00", "run": "5m"}]}
    site = Site(parsed(schedule=raw, limits=dict(BLOCK["limits"],
                                                 cooldown="1h")))
    site.step(90)
    site.step()
    site.step(10)
    site.step(15)
    site.shunt(-2.0 - 44.7)
    site.step()
    for _ in range(10):
        site.shunt(-2.0 - 44.7)
        site.step(60)
    assert site.persist["last_run_day"] == "2026-06-21"
    site.now = site.now.replace(hour=14, minute=0, second=30)
    site.shunt(-2.0)
    site.step()
    _, inverter = site.step()
    assert inverter.connected


def test_a_late_scheduled_start_runs_its_whole_length():
    raw = {"every_days": 1, "starts": [{"at": "11:00", "run": "20m"}]}
    site = Site(parsed(schedule=raw))
    site.step()
    run = site.state["irrigation"]["run"]
    assert run["ends_epoch"] - site.now.timestamp() == 20 * 60


def test_a_run_that_lost_the_clock_still_starts_the_cooldown():
    site = Site()
    checked_run(site)
    site.trusted = False
    site.step()
    site.step()
    assert site.persist.get("active_run"), "the run's end was dropped"
    site.trusted = True
    site.step()
    assert "active_run" not in site.persist
    assert site.persist["last_run_end_epoch"] == site.now.timestamp()


def test_a_run_whose_time_ends_while_starting_never_starts_the_pump():
    site = Site()
    site.force("irrigation", "on", 0.25)
    for _ in range(4):
        pump, _ = site.step()
        assert not pump.connected
