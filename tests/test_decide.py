"""The load decision, exercised where it is awkward rather than where it is easy.

Every clock here is injected. Nothing in these tests waits, and nothing reads
the time of day, so a battery can be walked down through every threshold and
back up again in microseconds and the answers are the same on any machine at
any hour.
"""

import datetime
import zoneinfo

import pytest

from monitoring.control import decide

ZONE = zoneinfo.ZoneInfo("America/Chicago")

#: A day in late June, which is where the solar windows are tightest.
DAY = datetime.date(2026, 6, 21)
SUNRISE = datetime.datetime(2026, 6, 21, 6, 10, tzinfo=ZONE)
SUNSET = datetime.datetime(2026, 6, 21, 20, 53, tzinfo=ZONE)
SUNS = {"sunrise": SUNRISE, "sunset": SUNSET}

HOUR = datetime.timedelta(hours=1)

#: The shipped camera ladder, as the node file declares it.
LADDER = {
    "source": {"device": "bms", "field": "state_of_charge_pct"},
    "stale_after": 600.0,
    "release_margin_pct": 15.0,
    "enter_after_readings": 2,
    "restore_dwell": 0.0,
    "floor": {
        "sources": [{"device": "bms", "field": "cell_min_volts",
                     "shed_below": 3.15, "restore_above": 3.25}],
    },
    "tiers": [
        {"below_pct": 50.0, "schedule": {"type": "clock",
                                         "on_from": 5 * 60, "on_to": 22 * 60}},
        {"below_pct": 25.0, "schedule": {
            "type": "solar",
            "on_windows": [
                {"from": ["sunrise", -HOUR], "to": ["sunrise", 2 * HOUR]},
                {"from": ["sunset", -2 * HOUR], "to": ["sunset", HOUR]},
            ]}},
        {"below_pct": 10.0, "schedule": {"type": "never"}},
    ],
}

#: The ladder with no minimum run or off time, which most tests want. A timer
#: holding a relay is correct behaviour and it would mask every assertion
#: about what the ladder decided, so the two are exercised separately.
SPEC = {"type": "dc_branch", "shed_ladder": LADDER,
        "min_run": 0.0, "min_off": 0.0}

#: The timings the node file actually ships, used only by the tests about
#: them.
TIMED = dict(SPEC, min_run=600.0, min_off=1800.0)

#: A floor with a second source on a different bus and in different units.
#: The battery arrives over Bluetooth from a device that is routinely quiet
#: for minutes, and the charge controller is wired, resident and reports every
#: thirty seconds. Each source carries its own bounds, because a cell voltage
#: and a pack voltage share no scale.
TWO_SOURCE = dict(LADDER, floor={"sources": [
    {"device": "bms", "field": "cell_min_volts",
     "shed_below": 3.15, "restore_above": 3.25},
    {"device": "mppt", "field": "battery_volts",
     "shed_below": 12.6, "restore_above": 13.0},
]})


def at(hour, minute=0):
    return datetime.datetime(2026, 6, 21, hour, minute, tzinfo=ZONE)


def reading(soc=None, cell=None, age=0.0, device="bms"):
    fields = {}
    if soc is not None:
        fields["state_of_charge_pct"] = soc
    if cell is not None:
        fields["cell_min_volts"] = cell
    return {device: {"fields": fields, "age_seconds": age}}


def run(soc=None, cell=3.30, now=None, state=None, switches=None,
        clock_trusted=True, mono=0.0, spec=None):
    """One pass of the whole decision for a single camera load."""
    decisions, new_state = decide.decide(
        now or at(12), mono, clock_trusted,
        reading(soc, cell), switches or {"shedding_enabled": True, "loads": {}},
        {"cameras": spec or SPEC}, state or {}, SUNS)
    return decisions["cameras"], new_state


#: An expiry comfortably ahead of every `at()` in this file, and one behind
#: it. Expressed against the same clock the schedules use, so a passing test
#: cannot depend on two clocks disagreeing.
LATER = at(23, 59).timestamp()
EARLIER = at(0, 1).timestamp()


def settle(soc, mono=0.0, **kwargs):
    """Feed the same reading until the latches stop moving.

    A tier needs consecutive readings to latch, so a test about what a tier
    does must first get the tier latched. Doing that explicitly here keeps
    every later assertion about the schedule rather than about the count.
    """
    state = kwargs.pop("state", {})
    decision = None
    for step in range(LADDER["enter_after_readings"]):
        decision, state = run(soc, state=state, mono=mono + step, **kwargs)
    return decision, state


# -- latching ---------------------------------------------------------------

def test_a_tier_needs_consecutive_readings_before_it_latches():
    """One glitched sample must not latch a tier whose release is fifteen
    points away, which in winter is weeks."""
    decision, state = run(45.0, now=at(23))
    assert decision.connected, "latched on a single reading"
    decision, state = run(45.0, now=at(23), state=state)
    assert not decision.connected


def test_a_reading_back_above_the_entry_resets_the_count():
    """Two readings below must mean two in a row, or a sensor flickering
    around the threshold latches on the second flicker."""
    _, state = run(45.0, now=at(23))
    _, state = run(60.0, now=at(23), state=state)
    decision, state = run(45.0, now=at(23), state=state)
    assert decision.connected, "the count survived a reading above entry"


@pytest.mark.parametrize("soc,latched", [
    (60.0, 0),
    (45.0, 1),
    (24.0, 2),
    (9.0, 3),
])
def test_the_latched_set_matches_the_state_of_charge(soc, latched):
    decision, _ = settle(soc)
    assert decision.tier == latched


def test_tiers_release_one_at_a_time_on_the_way_up():
    """The ratchet the owner asked for. Each tier holds until the source is
    fifteen points clear of its own entry, so recovering past one threshold
    does not release the ones below it."""
    _, state = settle(9.0)
    for soc, expected in ((22.0, 3), (26.0, 2), (41.0, 1), (66.0, 0)):
        decision, state = run(soc, state=state)
        assert decision.tier == expected, soc


def test_a_deeper_tier_is_never_latched_without_the_shallower_ones():
    """The invariant the intersection rule relies on: the latched set is a
    prefix of the ladder. It holds only because the tiers descend strictly
    and share one release margin."""
    state = {}
    for soc in (70.0, 60.0, 49.0, 40.0, 30.0, 24.0, 20.0, 11.0, 9.0, 5.0,
                12.0, 26.0, 30.0, 44.0, 52.0, 66.0, 80.0):
        for _ in range(2):
            _, state = run(soc, state=state)
        latched = [state["cameras"]["latched"].get(str(i), False)
                   for i in range(len(LADDER["tiers"]))]
        assert latched == sorted(latched, reverse=True), (soc, latched)


def test_a_falling_source_never_restores_power():
    """Monotonicity over a trajectory, which is the invariant that matters.
    Asserted over a walk rather than between two isolated calls, because the
    latches are what make it true and a stateless check would pass without
    them."""
    state = {}
    allowed = True
    for soc in (100.0, 80.0, 60.0, 50.0, 49.0, 40.0, 30.0, 26.0, 24.0, 20.0,
                12.0, 10.0, 9.0, 1.0):
        for _ in range(2):
            decision, state = run(soc, now=at(12), state=state)
        if not decision.connected:
            allowed = False
        assert decision.connected is False or allowed, (
            "power came back at %s while the source was still falling" % soc)


def test_a_source_exactly_at_a_tier_entry_does_not_latch_it():
    """Entry is strictly below. From an unlatched start, feeding the entry
    value itself must not latch, or every threshold is one point lower than
    the node file says."""
    state = {}
    for _ in range(LADDER["enter_after_readings"] + 1):
        decision, state = run(50.0, now=at(23), state=state)
    assert decision.connected, "latched at exactly its own entry"
    assert decision.tier == 0


def test_the_entry_and_release_of_adjacent_tiers_may_coincide():
    """With a margin of fifteen the ten tier releases above twenty-five and
    the twenty-five tier enters below it. At exactly twenty-five neither
    fires: the deeper tier stays latched and the shallower does not newly
    latch. The conservative answer, and the shipped ladder relies on the
    validator permitting it."""
    _, state = settle(9.0)
    decision, state = run(25.0, state=state)
    assert state["cameras"]["latched"]["2"] is True, "released at its boundary"
    decision, state = run(25.0, state=state)
    assert decision.tier == 3


def test_readings_either_side_of_a_stale_gap_are_not_consecutive():
    """Consecutive has to mean consecutive, or the requirement is none.

    With five minute readings and a ten minute staleness limit, a value below
    entry, a quiet radio, and the same value again would otherwise latch a
    tier on two samples that could be an hour apart.
    """
    fresh = {"bms": {"fields": {"state_of_charge_pct": 44.0},
                     "age_seconds": 1.0}}
    quiet = {"bms": {"fields": {"state_of_charge_pct": 44.0},
                     "age_seconds": 100_000.0}}
    state = {}
    for step, readings in enumerate((fresh, quiet, fresh)):
        decisions, state = decide.decide(
            at(23), float(step), True, readings,
            {"shedding_enabled": True, "loads": {}}, {"cameras": SPEC},
            state, SUNS)
    assert decisions["cameras"].connected, (
        "a tier latched on two readings separated by a stale gap")


def test_a_tier_releases_only_after_the_restore_dwell():
    """The failure this guards is restoring on a charging spike.

    A tier whose source jumps clear of its release for one reading has not
    recovered; it has seen the charger come on. The entry side is counted in
    readings and this side is timed, because a spike lasts seconds whatever
    the sensor rate.
    """
    patient = dict(LADDER, restore_dwell=900.0)
    spec = dict(SPEC, shed_ladder=patient)

    decision, state = settle(45.0, now=at(23), spec=spec)
    assert not decision.connected

    decision, state = run(66.0, now=at(23), mono=100.0, state=state, spec=spec)
    assert not decision.connected, "released on the first reading above"

    decision, state = run(66.0, now=at(23), mono=200.0, state=state, spec=spec)
    assert not decision.connected, "released before the dwell expired"

    decision, state = run(66.0, now=at(23), mono=1100.0, state=state,
                          spec=spec)
    assert decision.connected, "never released after the dwell expired"


def test_a_source_dipping_back_restarts_the_restore_dwell():
    """Otherwise the dwell measures from the first moment the source was ever
    clear, and a value flickering across the release would accumulate credit
    it has not earned."""
    patient = dict(LADDER, restore_dwell=900.0)
    spec = dict(SPEC, shed_ladder=patient)
    decision, state = settle(45.0, now=at(23), spec=spec)

    # Clear at 100, back below at 200, clear again at 300. A dwell that
    # restarted correctly now expires at 1200; one that kept the original
    # stamp would expire at 1000. The probe at 1100 is the only moment the
    # two answers differ, which is why it is there.
    for mono, soc in ((100.0, 66.0), (200.0, 55.0), (300.0, 66.0)):
        decision, state = run(soc, now=at(23), mono=mono, state=state,
                              spec=spec)

    decision, state = run(66.0, now=at(23), mono=1100.0, state=state,
                          spec=spec)
    assert not decision.connected, (
        "released at 1100, so the dwell was still measured from 100 and the "
        "dip below release did not restart it")

    decision, state = run(66.0, now=at(23), mono=1300.0, state=state,
                          spec=spec)
    assert decision.connected, "never released after the restarted dwell"


# -- the schedules ----------------------------------------------------------

@pytest.mark.parametrize("hour,minute,connected", [
    (4, 59, False),
    (5, 1, True),
    (12, 0, True),
    (21, 59, True),
    (22, 1, False),
])
def test_the_fifty_tier_runs_between_five_and_twenty_two(hour, minute,
                                                         connected):
    decision, _ = settle(45.0, now=at(hour, minute))
    assert decision.connected is connected


@pytest.mark.parametrize("when,connected", [
    (at(4, 0), False),
    (at(5, 0), False),       # the fifty tier permits it, the dawn window
                             # does not open until 05:10
    (at(5, 30), True),       # inside the dawn window
    (at(6, 30), True),       # inside it
    (at(9, 0), False),       # after it closes at 08:10
    (at(12, 0), False),      # midday is deliberately dark
    (at(19, 30), True),      # inside the dusk window, open from 18:53
    (at(21, 30), True),      # still inside, closes 21:53
    (at(23, 0), False),
])
def test_the_twenty_five_tier_runs_only_in_the_movement_windows(when,
                                                               connected):
    decision, _ = settle(24.0, now=when)
    assert decision.connected is connected


@pytest.mark.parametrize("hour,connected", [
    (21, False),     # before the window opens
    (22, True),      # it opens at 22:00
    (23, True),
    (0, True),       # across midnight
    (4, True),
    (5, False),      # it closes at 05:00
    (12, False),
])
def test_a_clock_window_may_wrap_midnight(hour, connected):
    """The shipped window runs 05:00 to 22:00 and does not wrap, so nothing
    else here reaches this branch. A window written the other way round means
    the two ends of the day rather than the gap between them, and reading it
    as a gap inverts the schedule for every hour.
    """
    wrapped = dict(LADDER)
    wrapped["tiers"] = [{"below_pct": 50.0,
                         "schedule": {"type": "clock",
                                      "on_from": 22 * 60, "on_to": 5 * 60}}]
    decision, _ = settle(45.0, now=at(hour), spec=dict(SPEC,
                                                       shed_ladder=wrapped))
    assert decision.connected is connected


def test_the_ten_tier_never_runs():
    for hour in (0, 6, 7, 12, 19, 20, 23):
        decision, _ = settle(9.0, now=at(hour))
        assert not decision.connected, hour


def test_every_latched_tier_must_permit_it_not_just_the_deepest():
    """Intersection rather than deepest-tier-decides.

    Needs a ladder whose deeper window reaches outside the shallower tier's
    permitted period, which the shipped config never does: its windows sit
    inside 05:00 to 22:00 with minutes to spare all year. So this uses a
    synthetic ladder, because a test built on the real one cannot tell the two
    rules apart.
    """
    narrow = dict(LADDER)
    narrow["tiers"] = [
        {"below_pct": 50.0, "schedule": {"type": "clock",
                                         "on_from": 8 * 60, "on_to": 20 * 60}},
        dict(LADDER["tiers"][1]),
        dict(LADDER["tiers"][2]),
    ]
    spec = dict(SPEC, shed_ladder=narrow)

    # 21:00 is inside the dusk window and outside the shallow tier's period.
    decision, _ = settle(24.0, now=at(21), spec=spec)
    assert not decision.connected, (
        "the deeper tier's window was honoured past the shallower tier's "
        "permitted period, which is more power at a lower state of charge")


# -- the floor --------------------------------------------------------------

def test_the_floor_sheds_whatever_the_tiers_say():
    decision, _ = run(100.0, cell=3.10, now=at(12))
    assert not decision.connected
    assert decision.reason_code == decide.FLOOR
    assert "3.150" in decision.reason or "3.15" in decision.reason


def test_the_floor_holds_until_a_reading_clears_it():
    """Released only strictly above the restoring bound, so a cell sagging
    under load cannot chatter the relay."""
    _, state = run(100.0, cell=3.10)
    decision, state = run(100.0, cell=3.20, state=state)
    assert not decision.connected, "released between the two bounds"
    decision, state = run(100.0, cell=3.30, state=state)
    assert decision.connected


def test_the_floor_survives_an_override():
    """A floor an override can defeat is not a floor."""
    switches = {"shedding_enabled": True,
                "loads": {"cameras": {"force": "on",
                                      "expires_epoch": LATER}}}
    decision, _ = run(100.0, cell=3.10, switches=switches)
    assert not decision.connected
    assert decision.reason_code == decide.FLOOR


def test_the_floor_survives_shedding_being_switched_off():
    decision, _ = run(100.0, cell=3.10,
                      switches={"shedding_enabled": False, "loads": {}})
    assert not decision.connected
    assert decision.reason_code == decide.FLOOR


def test_the_floor_ignores_the_minimum_run_time():
    """A minimum run time that held a load up while a cell sat under its
    floor would defeat the one rule with nothing above it."""
    _, state = run(100.0, cell=3.30, mono=0.0, spec=TIMED)
    decision, _ = run(100.0, cell=3.10, mono=1.0, state=state, spec=TIMED)
    assert not decision.connected


def test_the_floor_needs_no_clock():
    decision, _ = run(100.0, cell=3.10, clock_trusted=False)
    assert not decision.connected
    assert decision.reason_code == decide.FLOOR


def test_a_second_floor_source_can_shed_when_the_first_is_quiet():
    """The battery arrives over Bluetooth from a device that goes quiet for
    minutes. A ladder with one source hangs on the least reliable thing on
    the node, so an independently wired source can shed on its own."""
    spec = dict(SPEC, shed_ladder=TWO_SOURCE)
    readings = {"bms": {"fields": {}, "age_seconds": 9999.0},
                "mppt": {"fields": {"battery_volts": 12.1},
                         "age_seconds": 5.0}}
    decisions, _ = decide.decide(
        at(12), 0.0, True, readings,
        {"shedding_enabled": True, "loads": {}}, {"cameras": spec}, {}, SUNS)
    assert not decisions["cameras"].connected
    assert decisions["cameras"].reason_code == decide.FLOOR


def test_a_healthy_source_is_judged_against_its_own_bounds():
    """Each floor source carries its own, because they share no scale.

    One pair of bounds for both compares a perfectly good cell at 3.30
    against a pack threshold of 12.6 and sheds the cameras forever.
    """
    spec = dict(SPEC, shed_ladder=TWO_SOURCE)
    readings = {"bms": {"fields": {"state_of_charge_pct": 100.0,
                                   "cell_min_volts": 3.30},
                        "age_seconds": 1.0},
                "mppt": {"fields": {"battery_volts": 13.4},
                         "age_seconds": 1.0}}
    decisions, _ = decide.decide(
        at(12), 0.0, True, readings,
        {"shedding_enabled": True, "loads": {}}, {"cameras": spec}, {}, SUNS)
    assert decisions["cameras"].connected, decisions["cameras"].reason


def test_a_latched_floor_holds_when_its_source_goes_quiet():
    """Releasing a shed because a radio went quiet is the wrong direction.
    The floor holds with no time limit and says it is holding."""
    _, state = run(100.0, cell=3.10)
    decisions, state = decide.decide(
        at(12), 1.0, True,
        {"bms": {"fields": {}, "age_seconds": 1.0}},
        {"shedding_enabled": True, "loads": {}}, {"cameras": SPEC},
        state, SUNS)
    assert not decisions["cameras"].connected
    assert "nothing readable to clear it" in decisions["cameras"].reason


def test_a_stale_floor_reading_cannot_release_a_latched_floor():
    """The dangerous direction. A cell record frozen at 3.30 from before the
    sag would otherwise read as proof the pack had recovered."""
    _, state = run(100.0, cell=3.10)
    decisions, _ = decide.decide(
        at(12), 1.0, True,
        {"bms": {"fields": {"cell_min_volts": 3.30},
                 "age_seconds": 100_000.0}},
        {"shedding_enabled": True, "loads": {}}, {"cameras": SPEC},
        state, SUNS)
    assert not decisions["cameras"].connected


def test_a_value_between_the_floor_bounds_does_not_shed_from_an_unlatched_start():
    """The hysteresis is two thresholds, not one. Without the latch check a
    cell at 3.20 would shed on first sight and the restoring bound would
    become the shedding bound."""
    decision, _ = run(100.0, cell=3.20)
    assert decision.connected


# -- stale readings ---------------------------------------------------------

def test_a_stale_reading_holds_the_latches_and_keeps_the_schedule_running():
    """What holds is the battery judgment, not the schedule.

    Freezing the commanded state instead would leave the cameras off all
    morning after a pack went quiet at eleven at night, and running all night
    after one went quiet at nine in the evening.
    """
    _, state = settle(45.0, now=at(12))
    assert state["cameras"]["latched"]["0"] is True

    stale = {"bms": {"fields": {"state_of_charge_pct": 45.0},
                     "age_seconds": 100_000.0}}
    for when, connected in ((at(12), True), (at(23), False), (at(8), True)):
        decisions, state = decide.decide(
            when, 0.0, True, stale,
            {"shedding_enabled": True, "loads": {}}, {"cameras": SPEC},
            state, SUNS)
        assert decisions["cameras"].connected is connected, when
        assert state["cameras"]["latched"]["0"] is True, "a latch moved"


def test_a_stale_reading_is_not_read_as_zero():
    """Zero would latch every tier and shed the cameras on a quiet radio.

    Fed enough times to latch, because one pass counts one reading and could
    not latch a tier whatever it read.
    """
    stale = {"bms": {"fields": {"state_of_charge_pct": 45.0},
                     "age_seconds": 100_000.0}}
    state = {}
    for step in range(LADDER["enter_after_readings"] + 1):
        decisions, state = decide.decide(
            at(12), float(step), True, stale,
            {"shedding_enabled": True, "loads": {}}, {"cameras": SPEC},
            state, SUNS)
    assert decisions["cameras"].connected
    assert decisions["cameras"].tier == 0


def test_a_missing_field_in_a_fresh_reading_is_an_absence_not_a_zero():
    readings = {"bms": {"fields": {"cell_min_volts": 3.3}, "age_seconds": 1.0}}
    decisions, _ = decide.decide(
        at(23), 0.0, True, readings,
        {"shedding_enabled": True, "loads": {}}, {"cameras": SPEC}, {}, SUNS)
    assert decisions["cameras"].connected, "a missing field shed the load"


# -- the clock --------------------------------------------------------------

def test_an_untrusted_clock_suspends_the_schedules_but_not_the_floor_tier():
    """Cameras run, which is also their de-energized state, so nothing
    special happens at the relay. The ten tier still applies because it
    needs no clock."""
    decision, _ = settle(45.0, now=at(23), clock_trusted=False)
    assert decision.connected
    assert decision.reason_code == decide.SCHEDULE_SUSPENDED

    decision, _ = settle(9.0, now=at(23), clock_trusted=False)
    assert not decision.connected, "the never tier needs no clock"


# -- overrides and switches -------------------------------------------------

def test_an_override_beats_the_tiers():
    switches = {"shedding_enabled": True,
                "loads": {"cameras": {"force": "on",
                                      "expires_epoch": LATER}}}
    decision, _ = settle(9.0, switches=switches)
    assert decision.connected
    assert decision.reason_code == decide.OVERRIDE


def test_an_override_can_force_a_load_off():
    switches = {"shedding_enabled": True,
                "loads": {"cameras": {"force": "off",
                                      "expires_epoch": LATER}}}
    decision, _ = run(100.0, switches=switches)
    assert not decision.connected


def test_an_expired_override_is_gone():
    switches = {"shedding_enabled": True,
                "loads": {"cameras": {"force": "on",
                                      "expires_epoch": EARLIER}}}
    decision, _ = settle(9.0, switches=switches)
    assert not decision.connected, "an expired override still applied"


def test_an_override_with_no_expiry_is_treated_as_expired():
    """The dangerous one is the override somebody set during a hunt and
    forgot. The CLI refuses to write one without an expiry, and this is the
    second line of that defence."""
    switches = {"shedding_enabled": True,
                "loads": {"cameras": {"force": "on"}}}
    decision, _ = settle(9.0, switches=switches)
    assert not decision.connected


def test_while_the_clock_is_untrusted_force_off_applies_and_force_on_does_not():
    """An expiry cannot be judged against a clock that may be wrong. Both
    halves fail safe: a force-off saves power, and ignoring a force-on costs
    nothing because the load's default is already to run."""
    off = {"shedding_enabled": True,
           "loads": {"cameras": {"force": "off", "expires_epoch": EARLIER}}}
    decision, _ = run(100.0, switches=off, clock_trusted=False)
    assert not decision.connected

    on = {"shedding_enabled": True,
          "loads": {"cameras": {"force": "on", "expires_epoch": EARLIER}}}
    decision, _ = settle(9.0, switches=on, clock_trusted=False)
    assert not decision.connected, "a force-on was honoured on a bad clock"


def test_shedding_switched_off_stops_the_tiers():
    decision, _ = settle(9.0, switches={"shedding_enabled": False,
                                        "loads": {}})
    assert decision.connected
    assert decision.reason_code == decide.SHEDDING_DISABLED
    assert "shedding is switched off" in decision.reason


# -- the timers -------------------------------------------------------------

def _shed_with_timers():
    """Get a load actually off with the shipped timings in force.

    The minimum run time holds it up at first, so this walks past that before
    returning. A test that skipped it would be asserting about a load that
    never left the on state.
    """
    decision, state = settle(9.0, spec=TIMED)
    assert decision.connected, "expected the minimum run time to hold it up"
    decision, state = run(9.0, mono=700.0, state=state, spec=TIMED)
    assert not decision.connected, "the minimum run time never expired"
    return state


def test_a_minimum_off_time_holds_a_load_down():
    """A recorder should not be cycled repeatedly."""
    state = _shed_with_timers()
    decision, _ = run(100.0, mono=760.0, state=state, spec=TIMED)
    assert not decision.connected
    assert "minimum off time" in decision.reason


def test_each_transition_restarts_the_clock_on_the_next_one():
    """Without resetting the stamp at a transition every later timer measures
    from boot, so the second change of mind passes instantly and the recorder
    cycles."""
    state = _shed_with_timers()
    decision, state = run(100.0, mono=9000.0, state=state, spec=TIMED)
    assert decision.connected, "the minimum off time never expired"
    # Two readings to latch the tier, both well inside the minimum run time
    # that started when the load was restored at 9000.
    for mono in (9060.0, 9061.0):
        decision, state = run(9.0, mono=mono, state=state, spec=TIMED)
    assert decision.connected, (
        "shed 61s after restoring, so the minimum run time was measured from "
        "boot rather than from the restore")
    assert "minimum run time" in decision.reason


def test_a_minimum_off_time_expires():
    state = _shed_with_timers()
    decision, _ = run(100.0, mono=9000.0, state=state, spec=TIMED)
    assert decision.connected


def test_a_minimum_run_time_holds_a_load_up():
    _, state = run(100.0, mono=0.0, spec=TIMED)
    decision, _ = settle(9.0, mono=60.0, spec=TIMED, state=state)
    assert decision.connected
    assert "minimum run time" in decision.reason


# -- types this does not drive ----------------------------------------------

def test_a_load_type_this_does_not_drive_is_refused_rather_than_ignored():
    """A load nobody drives and nobody mentions is indistinguishable from one
    that is working."""
    decisions, _ = decide.decide(
        at(12), 0.0, True, reading(50.0),
        {"shedding_enabled": True, "loads": {}},
        {"irrigation": {"type": "sequenced_ac", "min_run": 0.0,
                        "min_off": 0.0}},
        {}, SUNS)
    assert not decisions["irrigation"].connected
    assert decisions["irrigation"].reason_code == decide.REFUSED
    assert "does not drive it" in decisions["irrigation"].reason
