"""Reading a control block, and refusing one that cannot be trusted.

The node file is edited by hand, on a laptop, about a machine an hour away,
and often while standing next to different hardware. So everything here
refuses rather than defaults. A control block that is wrong in a way this
cannot see is a relay doing the opposite of what somebody meant, discovered
weeks later from a gap in the data.

The rule throughout: a key nobody recognises is an error, not a comment. A
typo in a threshold name silently leaves the default in place, the load
behaves as though the line was never written, and the file reads as though it
was. A load type nobody recognises fails the same way and is worse: the type
yields no loads at all, so every test about that load passes by finding
nothing to check.
"""

import datetime

from ..duration import DurationError
from ..duration import seconds as _seconds
from . import relay

#: Load types a controller will drive. Anything else declared is refused at
#: load time rather than skipped at run time, because a load the file says is
#: enabled and nothing drives is indistinguishable from one that works.
DRIVEN_TYPES = ("dc_branch", "cpu_fan", "vent_fan", "battery_heating",
                "sequenced_ac")

#: Declared, specified, and deliberately not driven yet. An enabled load of
#: one of these types is refused.
DEFERRED_TYPES = ()

SCHEDULE_TYPES = ("clock", "solar", "never")

#: Events a solar window may be measured from.
SOLAR_EVENTS = ("sunrise", "sunset")

#: Keys a ladder load may carry. Deliberately only the ones something reads.
#:
#: `readback` and `on_stale_data` are specified in the node files and nothing
#: acts on either yet, so they are absent here and a load carrying one is
#: refused. Accepting a key nobody reads is the exact failure this module
#: exists to prevent: the line sits in the file looking like it is in force.
#: They go back in alongside the code that honours them.
LOAD_KEYS = {"type", "enabled", "control_pin", "polarity",
             "shed_ladder", "min_run", "min_off"}

#: Keys a CPU fan may carry. Its own set, because it shares nothing with a
#: ladder but the relay and the timers.
CPU_FAN_KEYS = {"type", "enabled", "control_pin", "polarity", "source",
                "stale_after", "run_above_f", "stop_below_f", "min_run",
                "min_off"}

#: Keys a vent fan may carry. It compares two sensors and waits out a dwell in
#: each direction, so it shares only the relay and the timers with the others.
VENT_FAN_KEYS = {"type", "enabled", "control_pin", "polarity", "on_stale_data",
                 "inside", "outside", "stale_after", "run_if_all",
                 "stop_if_any", "run_dwell", "stop_dwell", "min_run",
                 "min_off", "readback"}
VENT_RUN_KEYS = {"inside_above_f", "outside_cooler_by_f"}
VENT_STOP_KEYS = {"inside_below_f", "outside_cooler_by_less_than_f",
                  "outside_below_f"}

#: Keys a battery heater may carry. Its blocks are the ones the node files have
#: always specified, plus an optional preheat before sunrise.
HEATER_KEYS = {"type", "enabled", "control_pin", "polarity", "on_stale_data",
               "temperature", "heat_if_all", "stop_if_any", "heat_dwell",
               "stop_dwell", "min_run", "min_off", "require_production",
               "limits", "preheat", "readback"}
HEATER_TEMPERATURE_KEYS = {"sources", "fallback_sources", "max_disagreement_f",
                           "stale_after"}
HEATER_START_KEYS = {"coldest_below_f"}
HEATER_STOP_KEYS = {"coldest_above_f", "any_sensor_above_f"}
HEATER_PRODUCTION_KEYS = {"enabled", "source", "above_volts", "stale_after"}
HEATER_LIMIT_KEYS = {"max_run_per_day", "cooldown"}
PREHEAT_KEYS = {"enabled", "lead", "soc", "soc_above_pct", "forecast"}
PREHEAT_SOC_KEYS = {"sources", "stale_after"}
PREHEAT_FORECAST_KEYS = {"source", "above_wm2", "stale_after"}

#: Answers a heater may give for a temperature it cannot read. No default, since
#: they suit different hardware and the file has to say which it has.
HEATER_ON_STALE = ("fallback", "deny")

#: Seconds in a day, for the limits measured against one.
DAY_SECONDS = 86400.0

#: Keys the control block itself may carry. Owned by the loop rather than by
#: a load, and listed so a misspelled loop_interval is refused rather than
#: silently leaving the default in place.
#:
#: There is deliberately no key for where the loop's state goes. It belongs
#: in tmpfs and nowhere else, and the reason is in two halves. It is rewritten
#: every pass, so on the card it is thousands of fsynced writes a day on a
#: machine whose card is the one part with no spare. And it would buy nothing,
#: because the state carries the boot identity and a reader discards state
#: from a previous boot: the monotonic stamps inside are from a clock that has
#: since restarted at zero, so keeping them would hold a dwell for as long as
#: the machine had previously been up. A path here could only move the writes
#: onto the card in exchange for state that is thrown away on arrival.
CONTROL_KEYS = {"enabled", "loads", "loop_interval", "relay_trigger",
                "spare_pins"}

#: Keys this module answers for itself, with the reason an operator needs.
#:
#: Each one names something a node file may not decide. Refused rather than
#: ignored, and named rather than left to the general unknown-key message,
#: because somebody reading that message would go looking for a typo in a key
#: they spelled correctly.
RETIRED_CONTROL_KEYS = {
    "publish_as": "the controller's decisions reach the database through a "
                  "device declared like any other, so the tags they carry "
                  "belong on that device. Declare a device with driver: "
                  "control and move these tags to its tags block",
    "state_file": "the loop's state is held in tmpfs so it never reaches the "
                  "card, and it is discarded on reboot in any case because "
                  "its timers are measured on a clock that restarts at zero. "
                  "Remove the line",
    "clock": "what an untrusted clock does is fixed rather than chosen. "
             "Tiers whose schedule needs the time are suspended, tiers that "
             "never run still apply, the floor still applies, and a force-on "
             "is ignored until the clock can be trusted. Remove the block",
}
LADDER_KEYS = {"source", "stale_after", "release_margin_pct",
               "enter_after_readings", "restore_dwell", "floor", "tiers"}
FLOOR_KEYS = {"sources"}
FLOOR_SOURCE_KEYS = {"device", "field", "shed_below", "restore_above"}
SOURCE_KEYS = {"device", "field"}

#: Keys an irrigation load may carry, and the keys of each of its blocks.
IRRIGATION_KEYS = {"type", "enabled", "on_stale_data", "inverter", "pump",
                   "sources", "stale_after", "shunt_stale_after", "schedule",
                   "start_conditions", "continue_conditions", "limits",
                   "dry_run_check", "absolute_max_run", "state_file",
                   "floor"}
IRRIGATION_INVERTER_KEYS = {"control_pin", "polarity", "settle_after_on"}
IRRIGATION_PUMP_KEYS = {"control_pin", "polarity",
                        "settle_before_inverter_off", "readback"}
IRRIGATION_READBACK_KEYS = {"source", "pin", "expect_amps"}
IRRIGATION_SOURCES = ("soc", "volts", "shunt", "charge_state")
IRRIGATION_SCHEDULE_KEYS = {"every_days", "starts"}
IRRIGATION_START_KEYS = {"at", "run"}
IRRIGATION_START_CONDITION_KEYS = {"charge_state_was_in", "within",
                                   "soc_above_pct", "battery_volts_above"}
IRRIGATION_CONTINUE_KEYS = {"max_run", "soc_above_pct", "battery_volts_above"}
IRRIGATION_LIMIT_KEYS = {"cooldown", "max_amp_hours_per_day",
                         "failed_start_lockout"}
IRRIGATION_DRY_KEYS = {"settle", "deadline"}
TIER_KEYS = {"below_pct", "schedule"}
SCHEDULE_KEYS = {"type", "on_from", "on_to", "on_windows"}
WINDOW_KEYS = {"from", "to"}


class SpecError(ValueError):
    """A control block that will not be acted on."""


def _fail(where, message):
    raise SpecError("%s: %s" % (where, message))


def check_control_keys(control):
    """Refuse a control block whose own keys are wrong, and nothing more.

    Separate from the rest because it can be applied to every node file in
    the repository, including the ones that specify loads for hardware and
    load types this controller does not drive yet. Those cannot be fully
    validated, but their block-level keys can, and a wrong key there is the
    kind that lies quiet: the validator is otherwise reached only when a
    controller starts, a controller starts only where control is enabled, so
    a refused key sits in a file with control off until the day somebody
    turns it on, an hour from the nearest road.
    """
    _retired("control", control, RETIRED_CONTROL_KEYS)
    _only("control", control, CONTROL_KEYS)


def _retired(where, mapping, retired):
    if not isinstance(mapping, dict):
        return
    for key in sorted(set(mapping) & set(retired)):
        _fail(where, "%s is not read here: %s" % (key, retired[key]))


def _only(where, mapping, allowed):
    if not isinstance(mapping, dict):
        _fail(where, "expected a mapping, found %s" % type(mapping).__name__)
    unknown = set(mapping) - allowed
    if unknown:
        _fail(where, "unknown key(s) %s; a typo here leaves the default in "
                     "place and the file reads as though it did not"
                     % ", ".join(sorted(unknown)))


def _enabled(where, value):
    """Whether a load is on, refusing anything that is not a yes or a no.

    The same rule the device loader applies, and for the same reason: a loose
    test is the wrong way round for a setting whose whole purpose is to stop
    something. `enabled: "false"` is a non-empty string and would read as
    yes, and `enabled: 0` would read as no while looking like a typo for
    something else.
    """
    if not isinstance(value, bool):
        _fail(where, "enabled must be true or false, found %r; a string or a "
                     "number here would be read as a yes or a no by accident"
                     % (value,))
    return value


def _number(where, value, what):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _fail(where, "%s must be a number, found %r" % (what, value))
    value = float(value)
    # YAML spells these .nan and .inf. Every comparison against a NaN is
    # false, so a bound that is one passes every order check below and then
    # never trips.
    if value != value or value in (float("inf"), float("-inf")):
        _fail(where, "%s must be finite, found %r" % (what, value))
    return value


def _minutes(where, value, what):
    """A wall time written as HH:MM, as minutes after midnight."""
    if not isinstance(value, str) or ":" not in value:
        _fail(where, "%s must be a time written as HH:MM, found %r"
                     % (what, value))
    hours, _, minutes = value.partition(":")
    try:
        hour, minute = int(hours), int(minutes)
    except ValueError:
        _fail(where, "%s is not a time: %r" % (what, value))
    # Each part in range, not just their sum. Checking the total accepts
    # 12:75 as 13:15 and 23:60 as midnight, which is a typo read as a time.
    if not 0 <= hour <= 24 or not 0 <= minute < 60 or hour * 60 + minute > 24 * 60:
        _fail(where, "%s is not a time of day: %r" % (what, value))
    return hour * 60 + minute


def _duration(where, value, what, allow_negative=False, allow_zero=True):
    """A duration in seconds, refusing the signs that cannot be meant.

    Negatives are allowed by the parser because a solar window needs them,
    so the sign check belongs here where the meaning is known. A negative or
    zero staleness limit makes every reading stale forever, and a negative
    minimum time is a timer that has always expired.
    """
    try:
        seconds = _seconds(value, what)
    except DurationError as exc:
        _fail(where, str(exc))
    if not allow_negative and seconds < 0:
        _fail(where, "%s cannot be negative, found %r" % (what, value))
    if not allow_zero and seconds <= 0:
        _fail(where, "%s must be positive, found %r; a staleness limit of "
                     "zero makes every reading stale forever" % (what, value))
    return seconds


def _source(where, raw, devices):
    _only(where, raw, SOURCE_KEYS)
    for key in ("device", "field"):
        if not isinstance(raw.get(key), str) or not raw[key]:
            _fail(where, "needs a %s" % key)
    if raw["device"] not in devices:
        _fail(where, "names device %r, which this node does not declare, or "
                     "which is declared and disabled. Either way nothing will "
                     "ever write a reading for it, so nothing would ever "
                     "inform a decision and the controller would run and do "
                     "nothing. Known: %s"
                     % (raw["device"], ", ".join(sorted(devices)) or "none"))
    return {"device": raw["device"], "field": raw["field"]}


def _floor(where, raw, devices):
    _only(where, raw, FLOOR_KEYS)
    sources = raw.get("sources")
    if not isinstance(sources, list) or not sources:
        _fail(where, "needs at least one source")

    out = []
    for index, entry in enumerate(sources):
        at = "%s source %d" % (where, index)
        _only(at, entry, FLOOR_SOURCE_KEYS)
        source = _source(at, {"device": entry.get("device"),
                              "field": entry.get("field")}, devices)
        for key in ("shed_below", "restore_above"):
            if key not in entry:
                _fail(at, "needs %s; a floor with one bound is a threshold "
                          "that chatters" % key)
        shed = _number(at, entry["shed_below"], "shed_below")
        restore = _number(at, entry["restore_above"], "restore_above")
        if restore <= shed:
            _fail(at, "restore_above (%g) must be above shed_below (%g), or "
                      "the floor can never release" % (restore, shed))
        source["shed_below"] = shed
        source["restore_above"] = restore
        out.append(source)
    return {"sources": out}


def _schedule(where, raw):
    _only(where, raw, SCHEDULE_KEYS)
    kind = raw.get("type")
    if kind not in SCHEDULE_TYPES:
        _fail(where, "type must be one of %s, found %r"
                     % (", ".join(SCHEDULE_TYPES), kind))

    if kind == "never":
        if set(raw) - {"type"}:
            _fail(where, "a never schedule takes no other keys")
        return {"type": "never"}

    if kind == "clock":
        for key in ("on_from", "on_to"):
            if key not in raw:
                _fail(where, "a clock schedule needs %s" % key)
        if "on_windows" in raw:
            _fail(where, "a clock schedule takes no on_windows")
        start = _minutes(where, raw["on_from"], "on_from")
        end = _minutes(where, raw["on_to"], "on_to")
        if start == end:
            _fail(where, "on_from and on_to are both %s, which is either a "
                         "whole day or none of it" % raw["on_from"])
        return {"type": "clock", "on_from": start, "on_to": end}

    windows = raw.get("on_windows")
    if not isinstance(windows, list) or not windows:
        _fail(where, "a solar schedule needs at least one window")
    if {"on_from", "on_to"} & set(raw):
        _fail(where, "a solar schedule takes no on_from or on_to")

    out = []
    for index, window in enumerate(windows):
        at = "%s window %d" % (where, index)
        _only(at, window, WINDOW_KEYS)
        edges = {}
        for edge in ("from", "to"):
            if edge not in window:
                _fail(at, "needs a %s" % edge)
            pair = window[edge]
            if not isinstance(pair, list) or len(pair) != 2:
                _fail(at, "%s must be an event and an offset, such as "
                          "[sunrise, -1h]" % edge)
            event, offset = pair
            if event not in SOLAR_EVENTS:
                _fail(at, "%s names %r; it must be one of %s"
                          % (edge, event, ", ".join(SOLAR_EVENTS)))
            # A timedelta, not a number of seconds. Every other duration
            # here is compared against the monotonic clock and stays a
            # float, but a solar offset is added to a datetime, and adding a
            # float to a datetime raises rather than guessing at a unit.
            # Converting at the point where the unit is known keeps the
            # choice in one place.
            edges[edge] = (event, datetime.timedelta(
                seconds=_duration(at, offset, "%s offset" % edge,
                                  allow_negative=True)))
        if (edges["from"][0] == edges["to"][0]
                and edges["from"][1] >= edges["to"][1]):
            # Measured from the same event, so the comparison is exact. A
            # window that ends before it starts is never on, and the tier
            # then reads as scheduled while behaving as never.
            _fail(at, "the window ends at or before it starts, so it can "
                      "never be on")
        out.append(edges)
    return {"type": "solar", "on_windows": out}


def _ladder(where, raw, devices):
    _only(where, raw, LADDER_KEYS)
    # restore_dwell is required rather than defaulted to nothing. Entry
    # needs consecutive readings so one glitched sample cannot latch a tier;
    # release with no dwell lets one glitched sample clear every tier at
    # once, and the minimum run time then extends the mistake. A file that
    # wants no dwell can say zero and mean it.
    for key in ("source", "stale_after", "release_margin_pct", "tiers",
                "restore_dwell"):
        if key not in raw:
            _fail(where, "needs %s" % key)

    tiers = raw["tiers"]
    if not isinstance(tiers, list) or not tiers:
        _fail(where, "needs at least one tier")

    margin = _number(where, raw["release_margin_pct"], "release_margin_pct")
    if margin <= 0:
        _fail(where, "release_margin_pct must be positive, or a tier releases "
                     "at the value that latched it and the relay chatters")

    entered = raw.get("enter_after_readings", 2)
    if isinstance(entered, bool) or not isinstance(entered, int) or entered < 1:
        _fail(where, "enter_after_readings must be a whole number of at least "
                     "one, found %r" % (entered,))

    out_tiers = []
    previous = None
    for index, tier in enumerate(tiers):
        at = "%s tier %d" % (where, index)
        _only(at, tier, TIER_KEYS)
        for key in TIER_KEYS:
            if key not in tier:
                _fail(at, "needs %s" % key)
        below = _number(at, tier["below_pct"], "below_pct")
        if not 0 < below <= 100:
            _fail(at, "below_pct must be a percentage above zero, found %r"
                      % (tier["below_pct"],))
        if previous is not None and below >= previous:
            # The latched set is a prefix of the ladder only because the
            # tiers descend strictly and share one margin. Everything that
            # reasons about depth depends on it.
            _fail(at, "below_pct (%g) must be below the tier before it (%g); "
                      "the ladder has to descend strictly" % (below, previous))
        previous = below
        out_tiers.append({"below_pct": below,
                          "schedule": _schedule(at + " schedule",
                                                tier["schedule"])})

    return {
        "source": _source(where + " source", raw["source"], devices),
        "stale_after": _duration(where, raw["stale_after"], "stale_after",
                                 allow_zero=False),
        "release_margin_pct": margin,
        "enter_after_readings": entered,
        "restore_dwell": _duration(where, raw["restore_dwell"],
                                   "restore_dwell"),
        # A floor key present and empty is refused rather than read as no
        # floor: the likeliest cause is a half-deleted one, and silently
        # dropping the only rule with nothing above it is the wrong way to
        # resolve an ambiguity.
        "floor": (_floor(where + " floor", raw["floor"], devices)
                  if "floor" in raw else None),
        "tiers": out_tiers,
    }


def _fan(where, raw, devices):
    """A CPU fan's source and its two bounds.

    Two bounds, not one threshold, and strictly apart. A fan switched at a
    single temperature runs for one poll and stops for the next while the
    chip sits on that number.
    """
    for key in ("source", "stale_after", "run_above_f", "stop_below_f"):
        if key not in raw:
            _fail(where, "needs %s" % key)
    run = _number(where, raw["run_above_f"], "run_above_f")
    stop = _number(where, raw["stop_below_f"], "stop_below_f")
    if run <= stop:
        _fail(where, "run_above_f (%g) must be above stop_below_f (%g), or "
                     "the fan chatters at one temperature" % (run, stop))
    return {
        "source": _source(where + " source", raw["source"], devices),
        "stale_after": _duration(where, raw["stale_after"], "stale_after",
                                 allow_zero=False),
        "run_above_f": run,
        "stop_below_f": stop,
    }


def _vent_fan(where, raw, devices):
    """A vent fan's two sensors, its two conditions and its dwells.

    Each condition's bounds must leave a gap, or the fan chatters on a number
    the air can sit at for hours.
    """
    for key in ("inside", "outside", "stale_after", "run_if_all",
                "stop_if_any", "run_dwell", "stop_dwell"):
        if key not in raw:
            _fail(where, "needs %s" % key)
    if raw.get("on_stale_data", "deny") != "deny":
        _fail(where, "on_stale_data must be deny: with no temperature there "
                     "is nothing to say the air outside is any cooler")
    readback = raw.get("readback") or {}
    if readback.get("source", "none") != "none":
        _fail(where, "readback is not acted on, so its source must be none")
    if raw["inside"] == raw["outside"]:
        _fail(where, "inside and outside must be different sensors, or the "
                     "air can never read cooler")
    for key, allowed in (("run_if_all", VENT_RUN_KEYS),
                         ("stop_if_any", VENT_STOP_KEYS)):
        if not isinstance(raw[key], dict):
            _fail(where, "%s must be a mapping" % key)
        _only(where + " " + key, raw[key], allowed)
    run, stop = raw["run_if_all"], raw["stop_if_any"]
    for block, key in ((run, "inside_above_f"), (run, "outside_cooler_by_f"),
                       (stop, "inside_below_f"),
                       (stop, "outside_cooler_by_less_than_f")):
        if key not in block:
            _fail(where, "needs %s" % key)
    inside_run = _number(where, run["inside_above_f"], "inside_above_f")
    inside_stop = _number(where, stop["inside_below_f"], "inside_below_f")
    cooler_run = _number(where, run["outside_cooler_by_f"],
                         "outside_cooler_by_f")
    cooler_stop = _number(where, stop["outside_cooler_by_less_than_f"],
                          "outside_cooler_by_less_than_f")
    if inside_run <= inside_stop:
        _fail(where, "inside_above_f (%g) must be above inside_below_f (%g)"
                     % (inside_run, inside_stop))
    if cooler_run <= cooler_stop:
        _fail(where, "outside_cooler_by_f (%g) must be above "
                     "outside_cooler_by_less_than_f (%g)"
                     % (cooler_run, cooler_stop))
    floor = stop.get("outside_below_f")
    return {
        "inside": _source(where + " inside", raw["inside"], devices),
        "outside": _source(where + " outside", raw["outside"], devices),
        "stale_after": _duration(where, raw["stale_after"], "stale_after",
                                 allow_zero=False),
        "inside_above_f": inside_run,
        "inside_below_f": inside_stop,
        "outside_cooler_by_f": cooler_run,
        "outside_cooler_by_less_than_f": cooler_stop,
        "outside_below_f": (None if floor is None
                            else _number(where, floor, "outside_below_f")),
        "run_dwell": _duration(where, raw["run_dwell"], "run_dwell"),
        "stop_dwell": _duration(where, raw["stop_dwell"], "stop_dwell"),
    }


def _block(where, raw, key, allowed):
    """One required nested mapping, with its keys checked."""
    if key not in raw:
        _fail(where, "needs %s" % key)
    if not isinstance(raw[key], dict):
        _fail(where, "%s must be a mapping" % key)
    _only("%s %s" % (where, key), raw[key], allowed)
    return raw[key]


def _required(where, block, *keys):
    for key in keys:
        if key not in block:
            _fail(where, "needs %s" % key)


def _sources(where, raw, devices):
    """A non-empty list of sources, from a block's `sources` key."""
    sources = raw.get("sources")
    if not isinstance(sources, list) or not sources:
        _fail(where, "needs at least one entry in sources")
    return [_source("%s source %d" % (where, index), entry, devices)
            for index, entry in enumerate(sources)]


def _preheat(where, raw, devices):
    """The preheat before sunrise, or None when there is none.

    Switched off, it needs nothing else and names nothing, so it can be turned
    off in the file without the forecast device having to exist.
    """
    if "preheat" not in raw:
        return None
    block = _block(where, raw, "preheat", PREHEAT_KEYS)
    at = where + " preheat"
    if not _enabled(at, block.get("enabled", True)):
        return None
    _required(at, block, "lead", "soc", "soc_above_pct", "forecast")
    soc = _block(at, block, "soc", PREHEAT_SOC_KEYS)
    forecast = _block(at, block, "forecast", PREHEAT_FORECAST_KEYS)
    _required(at + " soc", soc, "sources", "stale_after")
    _required(at + " forecast", forecast, "source", "above_wm2", "stale_after")

    floor = _number(at, block["soc_above_pct"], "soc_above_pct")
    if not 0 <= floor < 100:
        _fail(at, "soc_above_pct must be a percentage below 100, found %r"
                  % (block["soc_above_pct"],))
    above = _number(at, forecast["above_wm2"], "above_wm2")
    if above < 0:
        _fail(at, "above_wm2 cannot be negative, found %g" % above)

    lead = _duration(at, block["lead"], "lead", allow_zero=False)
    aged = _duration(at, forecast["stale_after"], "forecast stale_after",
                     allow_zero=False)
    # The reading says nothing about which sunrise it describes. Kept under a
    # day together, the oldest forecast believed at the start of the lead was
    # still taken after the sunrise before this one, so this is the one it is
    # about.
    if lead + aged >= DAY_SECONDS:
        _fail(at, "the forecast's stale_after plus the lead must stay under a "
                  "day, or a forecast about an earlier sunrise could pass for "
                  "this one")
    return {
        # A timedelta, because it is taken from a sunrise, as a solar window's
        # offset is.
        "lead": datetime.timedelta(seconds=lead),
        "soc_sources": _sources(at + " soc", soc, devices),
        "soc_stale_after": _duration(at, soc["stale_after"], "soc stale_after",
                                     allow_zero=False),
        "soc_above_pct": floor,
        "forecast_source": _source(at + " forecast source", forecast["source"],
                                   devices),
        "forecast_above_wm2": above,
        "forecast_stale_after": aged,
    }


def _battery_heater(where, raw, devices):
    """A battery heater's sensors, bounds, gates, limits and preheat.

    Three things are refused here because a heater quietly doing the other thing
    is the failure they would hide: a production gate switched off, which would
    run it all night; a fallback source, which nothing reads; and a readback,
    because heat tape with its own thermostat draws nothing for much of the time
    it is commanded, so a heater drawing nothing is not evidence of a fault.
    """
    if "on_stale_data" not in raw:
        _fail(where, "needs on_stale_data: one of %s" % ", ".join(HEATER_ON_STALE))
    if raw["on_stale_data"] not in HEATER_ON_STALE:
        _fail(where, "on_stale_data must be one of %s, found %r"
                     % (", ".join(HEATER_ON_STALE), raw["on_stale_data"]))
    readback = raw.get("readback") or {}
    if not isinstance(readback, dict) or readback.get("source", "none") != "none":
        _fail(where, "readback is not acted on, so its source must be none")

    temperature = _block(where, raw, "temperature", HEATER_TEMPERATURE_KEYS)
    start = _block(where, raw, "heat_if_all", HEATER_START_KEYS)
    stop = _block(where, raw, "stop_if_any", HEATER_STOP_KEYS)
    production = _block(where, raw, "require_production",
                        HEATER_PRODUCTION_KEYS)
    limits = _block(where, raw, "limits", HEATER_LIMIT_KEYS)
    _required(where, raw, "heat_dwell", "stop_dwell")
    _required(where + " temperature", temperature, "sources",
              "max_disagreement_f", "stale_after")
    _required(where, start, "coldest_below_f")
    _required(where, stop, "coldest_above_f", "any_sensor_above_f")
    _required(where + " require_production", production, "source",
              "above_volts", "stale_after")
    _required(where + " limits", limits, "max_run_per_day", "cooldown")

    if temperature.get("fallback_sources"):
        _fail(where, "fallback_sources must be empty: nothing reads them, and "
                     "with no temperature this heats on production alone")
    if not _enabled(where + " require_production",
                    production.get("enabled", True)):
        _fail(where, "require_production cannot be switched off: with nothing "
                     "to say the sun is up, the heater would run all night")

    low = _number(where, start["coldest_below_f"], "coldest_below_f")
    high = _number(where, stop["coldest_above_f"], "coldest_above_f")
    hard = _number(where, stop["any_sensor_above_f"], "any_sensor_above_f")
    if high <= low:
        _fail(where, "coldest_above_f (%g) must be above coldest_below_f (%g), "
                     "or the heater chatters at one temperature" % (high, low))
    if hard <= high:
        _fail(where, "any_sensor_above_f (%g) must be above coldest_above_f "
                     "(%g), or the hard stop is the working stop" % (hard, high))
    disagreement = _number(where, temperature["max_disagreement_f"],
                           "max_disagreement_f")
    if disagreement <= 0:
        _fail(where, "max_disagreement_f must be positive, or two good sensors "
                     "always count as disagreeing")

    cap = _duration(where, limits["max_run_per_day"], "max_run_per_day",
                    allow_zero=False)
    if cap > DAY_SECONDS:
        _fail(where, "max_run_per_day cannot be more than a day, or it never "
                     "limits anything")
    return {
        "on_stale_data": raw["on_stale_data"],
        "sources": _sources(where + " temperature", temperature, devices),
        "stale_after": _duration(where, temperature["stale_after"],
                                 "stale_after", allow_zero=False),
        "max_disagreement_f": disagreement,
        "coldest_below_f": low,
        "coldest_above_f": high,
        "any_sensor_above_f": hard,
        "heat_dwell": _duration(where, raw["heat_dwell"], "heat_dwell"),
        "stop_dwell": _duration(where, raw["stop_dwell"], "stop_dwell"),
        "production": {
            "source": _source(where + " require_production source",
                              production["source"], devices),
            "above_volts": _number(where, production["above_volts"],
                                   "above_volts"),
            "stale_after": _duration(where, production["stale_after"],
                                     "production stale_after",
                                     allow_zero=False),
        },
        "max_run_per_day": cap,
        "cooldown": _duration(where, limits["cooldown"], "cooldown"),
        "preheat": _preheat(where, raw, devices),
    }


def _relay_part(where, raw, keys):
    _only(where, raw, keys)
    pin = raw.get("control_pin")
    if isinstance(pin, bool) or not isinstance(pin, int):
        _fail(where, "needs a control_pin, found %r" % (pin,))
    if raw.get("polarity") != "energize_to_connect":
        # A dead controller must leave the pump and the inverter off.
        _fail(where, "polarity must be energize_to_connect, so a dead "
                     "controller leaves it off")
    return {"control_pin": pin, "polarity": raw["polarity"]}


def _irrigation_start_at(where, value):
    if isinstance(value, str):
        return ("clock", _minutes(where, value, "at"))
    if (isinstance(value, list) and len(value) == 2
            and value[0] in SOLAR_EVENTS):
        return ("solar", value[0], datetime.timedelta(seconds=_duration(
            where, value[1], "at offset", allow_negative=True)))
    _fail(where, "at must be a time written as HH:MM, or an event and an "
                 "offset such as [sunrise, 1h], found %r" % (value,))


def _sequenced_ac(name, where, raw, devices):
    """An irrigation load: an inverter, a pump behind it, and its rules."""
    from monitoring.drivers import vedirect
    if raw.get("on_stale_data", "deny") != "deny":
        _fail(where, "on_stale_data must be deny: irrigation does not run "
                     "on a reading it cannot trust")

    def block(key, keys):
        value = raw.get(key)
        if not isinstance(value, dict):
            _fail(where, "needs a %s block" % key)
        _only("%s %s" % (where, key), value, keys)
        return value

    def need(at, mapping, key):
        if key not in mapping:
            _fail(at, "needs %s" % key)
        return mapping[key]

    inverter_raw = block("inverter", IRRIGATION_INVERTER_KEYS)
    inverter = _relay_part(where + " inverter", inverter_raw,
                           IRRIGATION_INVERTER_KEYS)
    # Not zero: the pump must start on a later pass than the inverter.
    inverter["settle"] = _duration(where + " inverter", need(
        where + " inverter", inverter_raw, "settle_after_on"),
        "settle_after_on", allow_zero=False)

    pump_raw = block("pump", IRRIGATION_PUMP_KEYS)
    pump = _relay_part(where + " pump", pump_raw, IRRIGATION_PUMP_KEYS)
    pump["settle_off"] = _duration(where + " pump", need(
        where + " pump", pump_raw, "settle_before_inverter_off"),
        "settle_before_inverter_off")
    readback = pump_raw.get("readback")
    at = where + " pump readback"
    _only(at, readback, IRRIGATION_READBACK_KEYS)
    if readback.get("source") != "load" or readback.get("pin") is not None:
        _fail(at, "source must be load and pin null: the pump is checked on "
                  "the battery current the shunt measures")
    expect = need(at, readback, "expect_amps")
    _only(at + " expect_amps", expect, {"min", "max"})
    low = _number(at, need(at, expect, "min"), "expect_amps min")
    high = _number(at, need(at, expect, "max"), "expect_amps max")
    if not 0 < low < high:
        _fail(at, "expect_amps needs 0 < min < max")
    pump["expect_amps"] = (low, high)

    sources_raw = block("sources", set(IRRIGATION_SOURCES))
    sources = {}
    for key in IRRIGATION_SOURCES:
        sources[key] = _source("%s sources %s" % (where, key),
                               need(where + " sources", sources_raw, key),
                               devices)

    schedule = None
    schedule_raw = raw.get("schedule")
    if schedule_raw not in (None, [], {}):
        at = where + " schedule"
        _only(at, schedule_raw, IRRIGATION_SCHEDULE_KEYS)
        every = need(at, schedule_raw, "every_days")
        if isinstance(every, bool) or not isinstance(every, int) or every < 1:
            _fail(at, "every_days must be a whole number of days, 1 or more")
        starts = need(at, schedule_raw, "starts")
        if not isinstance(starts, list) or not starts:
            _fail(at, "needs at least one start")
        parsed = []
        for index, entry in enumerate(starts):
            here = "%s start %d" % (at, index)
            _only(here, entry, IRRIGATION_START_KEYS)
            parsed.append({
                "at": _irrigation_start_at(here, need(here, entry, "at")),
                "run": _duration(here, need(here, entry, "run"), "run",
                                 allow_zero=False)})
        schedule = {"every_days": every, "starts": parsed}

    start_raw = block("start_conditions", IRRIGATION_START_CONDITION_KEYS)
    at = where + " start_conditions"
    states = need(at, start_raw, "charge_state_was_in")
    codes = {label: code for code, label in vedirect.CHARGE_STATE.items()}
    if not isinstance(states, list) or not states or any(
            state not in codes for state in states):
        _fail(at, "charge_state_was_in must list charge states from: %s"
                  % ", ".join(sorted(codes)))
    start = {
        "charge_state_names": list(states),
        "charge_state_codes": {codes[state] for state in states},
        "charge_state_within": _duration(at, need(at, start_raw, "within"),
                                         "within", allow_zero=False),
        "soc_above": _number(at, need(at, start_raw, "soc_above_pct"),
                             "soc_above_pct"),
        "volts_above": _number(at, need(at, start_raw,
                                        "battery_volts_above"),
                               "battery_volts_above"),
    }

    keep_raw = block("continue_conditions", IRRIGATION_CONTINUE_KEYS)
    at = where + " continue_conditions"
    keep = {
        "max_run": _duration(at, need(at, keep_raw, "max_run"), "max_run",
                             allow_zero=False),
        "soc_above": _number(at, need(at, keep_raw, "soc_above_pct"),
                             "soc_above_pct"),
        "volts_above": _number(at, need(at, keep_raw, "battery_volts_above"),
                               "battery_volts_above"),
    }
    if keep["soc_above"] >= start["soc_above"] or (
            keep["volts_above"] >= start["volts_above"]):
        # The start minimums are also where an inverter held off by the
        # battery comes back on, so they must sit above the stops.
        _fail(at, "the continue minimums must be below the start minimums")

    limits_raw = block("limits", IRRIGATION_LIMIT_KEYS)
    at = where + " limits"
    lockout = need(at, limits_raw, "failed_start_lockout")
    if isinstance(lockout, bool) or not isinstance(lockout, int) or \
            lockout < 1:
        _fail(at, "failed_start_lockout must be a whole number, 1 or more")
    limits = {
        "cooldown": _duration(at, need(at, limits_raw, "cooldown"),
                              "cooldown"),
        "max_amp_hours": _number(at, need(at, limits_raw,
                                          "max_amp_hours_per_day"),
                                 "max_amp_hours_per_day"),
        "failed_start_lockout": lockout,
    }

    dry_raw = block("dry_run_check", IRRIGATION_DRY_KEYS)
    at = where + " dry_run_check"
    dry = {"settle": _duration(at, need(at, dry_raw, "settle"), "settle"),
           "deadline": _duration(at, need(at, dry_raw, "deadline"),
                                 "deadline", allow_zero=False)}

    absolute = _duration(where, need(where, raw, "absolute_max_run"),
                         "absolute_max_run", allow_zero=False)
    if keep["max_run"] > absolute:
        _fail(where, "max_run cannot be longer than absolute_max_run")
    state_file = raw.get("state_file")
    if not isinstance(state_file, str) or not state_file.startswith("/"):
        _fail(where, "state_file must be an absolute path")

    return {
        "name": name, "type": "sequenced_ac",
        "inverter": inverter, "pump": pump, "sources": sources,
        "stale_after": _duration(where, need(where, raw, "stale_after"),
                                 "stale_after", allow_zero=False),
        "shunt_stale_after": _duration(
            where, need(where, raw, "shunt_stale_after"),
            "shunt_stale_after", allow_zero=False),
        "schedule": schedule, "start": start, "continue": keep,
        "limits": limits, "dry_run_check": dry,
        "absolute_max_run": absolute, "state_file": state_file,
        # The node's battery floor, the one the camera branches have. A
        # cell under it stops irrigation and the inverter however the pack
        # voltage and the charge read.
        "floor": _floor(where + " floor", need(where, raw, "floor"),
                        devices),
        # Not timed by the minimum run and off times: the sequence times
        # itself.
        "min_run": 0.0, "min_off": 0.0,
    }


def load_spec(name, raw, devices):
    """Parse one load. Returns the spec a decision can act on."""
    where = "load %s" % name
    if not isinstance(raw, dict):
        _fail(where, "expected a mapping, found %s" % type(raw).__name__)

    kind = raw.get("type")

    # The type and the enable are read before the keys are checked, so a
    # deferred load can keep the specification its deferral exists to
    # preserve. Checking keys first would refuse the irrigation block for
    # declaring an inverter and a pump, and the only accepted form would be
    # the load stripped to two lines, which deletes the work.
    if kind in DEFERRED_TYPES:
        if _enabled(where, raw.get("enabled", True)):
            _fail(where, "type %r is specified but not driven yet, so it must "
                         "be enabled: false with the reason in a comment. An "
                         "enabled load nothing drives is indistinguishable "
                         "from one that works" % kind)
        return None
    if kind not in DRIVEN_TYPES:
        _fail(where, "type %r is not a type this controller drives; known: %s"
                     % (kind, ", ".join(DRIVEN_TYPES + DEFERRED_TYPES)))
    if kind == "sequenced_ac":
        _only(where, raw, IRRIGATION_KEYS)
        if not _enabled(where, raw.get("enabled", True)):
            return None
        return _sequenced_ac(name, where, raw, devices)

    _only(where, raw, {"cpu_fan": CPU_FAN_KEYS,
                       "vent_fan": VENT_FAN_KEYS,
                       "battery_heating": HEATER_KEYS}.get(kind, LOAD_KEYS))
    if not _enabled(where, raw.get("enabled", True)):
        return None

    pin = raw.get("control_pin")
    if isinstance(pin, bool) or not isinstance(pin, int):
        _fail(where, "needs a control_pin, found %r" % (pin,))

    polarity = raw.get("polarity")
    if polarity not in relay.POLARITIES:
        _fail(where, "polarity must be one of %s; it decides what a dead "
                     "controller leaves behind and there is no safe default"
                     % ", ".join(relay.POLARITIES))

    out = {
        "name": name,
        "type": kind,
        "control_pin": pin,
        "polarity": polarity,
        "min_run": _duration(where, raw.get("min_run", 0), "min_run"),
        "min_off": _duration(where, raw.get("min_off", 0), "min_off"),
    }
    if kind == "cpu_fan":
        out.update(_fan(where, raw, devices))
        return out
    if kind == "vent_fan":
        out.update(_vent_fan(where, raw, devices))
        return out
    if kind == "battery_heating":
        out.update(_battery_heater(where, raw, devices))
        return out

    if "shed_ladder" not in raw:
        _fail(where, "needs a shed_ladder")
    out["shed_ladder"] = _ladder(where + " shed_ladder", raw["shed_ladder"],
                                 devices)
    return out


def load_control(node):
    """Every load on a node that a controller should drive.

    Refuses the whole block rather than dropping the load it cannot read. A
    controller running three loads out of four, on a machine nobody can
    reach, is worse than one that refuses to start and says why.
    """
    check_control_keys(node.control)
    # Enabled devices only. A disabled one never writes the cache, so a
    # ladder naming it would wait forever on a reading that cannot come.
    devices = {device.name for device in node.devices if device.enabled}
    loads = {}
    for name in sorted((node.control.get("loads") or {})):
        spec = load_spec(name, node.control["loads"][name], devices)
        if spec is not None:
            loads[name] = spec
    if loads:
        trigger = node.control.get("relay_trigger")
        if isinstance(trigger, bool) or trigger not in relay.TRIGGERS:
            _fail("control", "relay_trigger must be one of %s, found %r. It "
                             "says whether the relay board energizes a coil on "
                             "a high input or a low one, the two drive every "
                             "load in opposite directions, and there is no "
                             "safe default" % (", ".join(relay.TRIGGERS),
                                               trigger))
        for spec in loads.values():
            spec["trigger"] = trigger
    return loads


def outputs(load):
    """The relays one load drives, as (relay name, pin, polarity).

    One for most loads. Two for an irrigation load: its inverter, under the
    load's name with ".inverter" added, and its pump, under the load's name.
    """
    if load["type"] == "sequenced_ac":
        return [(load["name"] + ".inverter", load["inverter"]["control_pin"],
                 load["inverter"]["polarity"]),
                (load["name"], load["pump"]["control_pin"],
                 load["pump"]["polarity"])]
    return [(load["name"], load["control_pin"], load["polarity"])]
