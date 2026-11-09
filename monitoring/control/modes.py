"""Plain words for what each load is set to do, for the override page.

Each load gets a mode line, which says what it is doing in a few words, and
a plan line, which says what the node file tells it to do in every case. A
person reading the page on a phone sees from the mode whether the load is
where they expect it, and from the plan what comes next. When the mode alone
cannot say why, a third line gives the controller's own reason.

Nothing here decides anything. The words come from the node file and from
what the loop last published, so they can never disagree with the loop.
"""

import math

from monitoring.control import decide, irrigation

#: The phase an irrigation load publishes, as a number, back to its name.
PHASES = {code: name for name, code in irrigation.PHASE_CODES.items()}

#: Reason codes whose cause the mode line does not carry, so the page shows
#: the controller's reason beside it.
UNEXPLAINED = (decide.HOLDING_STALE, decide.NO_READING, decide.REFUSED,
               decide.HELD_BY_TIMER, decide.RUN_REFUSED, decide.FAILED_START)


class Mode:
    """The lines one relay's row shows."""

    def __init__(self, mode, plan, why=None, beats_force=False):
        self.mode = mode
        self.plan = plan
        self.why = why
        #: Whether the mode holds even against a force, as the battery floor
        #: does, so the page shows it as now and not as what comes next.
        self.beats_force = beats_force


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value if math.isfinite(value) else None


def _entry(published, name):
    entry = published.get(name)
    return entry if isinstance(entry, dict) else {}


def _clock(minutes):
    return "%02d:%02d" % divmod(int(minutes) % 1440, 60)


def _covers(schedule, minute):
    start, end = schedule["on_from"], schedule["on_to"]
    if start < end:
        return start <= minute < end
    return minute >= start or minute < end


def _edge(edge):
    event, delta = edge
    seconds = int(delta.total_seconds())
    if seconds == 0:
        return event
    hours, rest = divmod(abs(seconds), 3600)
    amount = "%d min" % (abs(seconds) // 60) if rest else "%d h" % hours
    return "%s %s %s" % (amount, "before" if seconds < 0 else "after", event)


def _window_kind(window):
    return {("sunrise", "sunrise"): "dawn", ("sunset", "sunset"): "dusk",
            ("sunrise", "sunset"): "daylight",
            ("sunset", "sunrise"): "overnight"}[
        (window["from"][0], window["to"][0])]


def _solar_name(schedule):
    kinds = {_window_kind(window) for window in schedule["on_windows"]}
    return {frozenset(("dawn", "dusk")): "dawn and dusk only",
            frozenset(("dawn",)): "mornings only",
            frozenset(("dusk",)): "evenings only",
            frozenset(("daylight",)): "daylight only",
            frozenset(("overnight",)): "overnight only"}.get(
        frozenset(kinds), "set solar hours only")


def _clock_name(schedule):
    noon, midnight = _covers(schedule, 720), _covers(schedule, 0)
    if noon and not midnight:
        return "day only"
    if midnight and not noon:
        return "night only"
    return None


def _hours(schedule):
    if schedule["type"] == "clock":
        return "on %s to %s" % (_clock(schedule["on_from"]),
                                _clock(schedule["on_to"]))
    return "on " + ", and ".join(
        "from %s to %s" % (_edge(w["from"]), _edge(w["to"]))
        for w in schedule["on_windows"])


def schedule_mode(schedule):
    """The mode line for one battery step's schedule."""
    kind = schedule["type"]
    if kind == "never":
        return "Off to save battery"
    if kind == "clock":
        name = _clock_name(schedule)
        if name == "day only":
            return "Day only (%s, off at night)" % _hours(schedule)
        if name == "night only":
            return "Night only (%s, off by day)" % _hours(schedule)
        return "Set hours only (%s)" % _hours(schedule)
    return "%s (%s)" % (_solar_name(schedule).capitalize(), _hours(schedule))


def _schedule_short(schedule):
    kind = schedule["type"]
    if kind == "never":
        return "off"
    if kind == "clock":
        return _clock_name(schedule) or _hours(schedule)
    return _solar_name(schedule).replace(" only", "")


def _reading(readings, source, stale_after):
    """A reading the controller would still act on, else None."""
    if not isinstance(source, dict):
        return None
    reading = readings.get(source.get("device"))
    if not isinstance(reading, dict):
        return None
    age = _number(reading.get("age_seconds"))
    if age is None or age > stale_after:
        return None
    return _number((reading.get("fields") or {}).get(source.get("field")))


def _temperature(value):
    return "no reading" if value is None else "%.0f F" % value


def _why(own, always=False):
    reason = own.get("reason")
    if not isinstance(reason, str) or not reason:
        return None
    if always or own.get("reason_code") in UNEXPLAINED:
        return reason
    return None


def _ladder(load, own, readings, shedding):
    ladder = load["shed_ladder"]
    tiers = ladder["tiers"]
    battery = _reading(readings, ladder["source"], ladder["stale_after"])
    steps = " ".join("Below %g%%: %s." % (tier["below_pct"],
                                         _schedule_short(tier["schedule"]))
                     for tier in tiers)
    plan = "Battery %s. %s" % ("no reading" if battery is None
                                else "%.0f%%" % battery, steps)
    code = own.get("reason_code")
    level = _number(own.get("shed_level"))
    if code == decide.FLOOR or own.get("floor_latched"):
        return Mode("Off: battery at its floor", plan, _why(own),
                    beats_force=True)
    if not shedding:
        mode = "Always on: shedding paused"
    elif level is None:
        mode = "Mode unknown: nothing published yet"
    else:
        mode = _latched_mode(tiers[:max(0, min(int(level), len(tiers)))])
        if code == decide.SCHEDULE_SUSPENDED:
            mode += ". Held on: the clock is not trusted"
    if code == decide.HELD_BY_TIMER:
        mode += ". Held %s for now" % ("on" if own.get("powered") else "off")
    return Mode(mode, plan, _why(own))


def _latched_mode(latched):
    """The mode while these steps are latched.

    The load runs only while every latched step allows it, so the deepest
    step is named and each shallower step whose hours can cut it further is
    named after it.
    """
    if not latched:
        return "Always on"
    if any(tier["schedule"]["type"] == "never" for tier in latched):
        return "Off to save battery"
    deepest = latched[-1]["schedule"]
    mode = schedule_mode(deepest)
    seen = [deepest]
    for tier in reversed(latched[:-1]):
        schedule = tier["schedule"]
        if schedule in seen:
            continue
        seen.append(schedule)
        mode += ", and only while %s" % _hours(schedule).replace("on ", "", 1)
    return mode


def _state(own, what, forced):
    if forced:
        return None
    powered = own.get("powered")
    if powered is None:
        return "state unknown"
    return what if powered else "idle"


def _join(*parts):
    return ", ".join(part for part in parts if part)


def _cpu_fan(load, own, readings, forced):
    value = _reading(readings, load["source"], load["stale_after"])
    return Mode(_join("By temperature", _state(own, "running", forced),
                      _temperature(value)),
                "Runs at %g F or above, stops at %g F or below." % (
                    load["run_above_f"], load["stop_below_f"]),
                _why(own))


def _vent_fan(load, own, readings, forced):
    inside = _reading(readings, load["inside"], load["stale_after"])
    outside = _reading(readings, load["outside"], load["stale_after"])
    plan = ("Runs when inside is at %g F or above and outside is at least "
            "%g F cooler. Stops when inside is at %g F or below or outside "
            "is %g F cooler or less." % (load["inside_above_f"], load["outside_cooler_by_f"],
                         load["inside_below_f"],
                         load["outside_cooler_by_less_than_f"]))
    if load.get("outside_below_f") is not None:
        plan += " Stops when outside is at %g F or below." % (
            load["outside_below_f"])
    return Mode(_join("By temperature", _state(own, "running", forced),
                      "inside " + _temperature(inside),
                      "outside " + _temperature(outside)), plan, _why(own))


def _heater(load, own, readings, forced):
    known = [value for value in (
        _reading(readings, source, load["stale_after"])
        for source in load["sources"]) if value is not None]
    coldest = min(known) if known else None
    return Mode(_join("By temperature", _state(own, "heating", forced),
                      "coldest sensor " + _temperature(coldest)),
                "Heats at %g F or below, stops at %g F or above, within its "
                "power and daily limits." % (load["coldest_below_f"],
                                   load["coldest_above_f"]),
                _why(own, always=not forced))


def _run(load, own, inverter, forced):
    if load.get("schedule"):
        idle = "Idle: waiting for the next scheduled start"
        plan = "Runs every %d days." % load["schedule"]["every_days"]
    else:
        idle = "Idle: manual only"
        plan = "Runs only when started here."
    plan += (" A run stops at every safety limit: a dry or stalled pump, "
             "the battery, the run limit.")
    if own.get("locked_out"):
        return Mode("Locked out: too many failed starts", plan, _why(own, True))
    if forced:
        return Mode(idle, plan)
    phase = PHASES.get(_number(own.get("phase")))
    if phase is None:
        return Mode("Mode unknown: nothing published yet", plan)
    mode = {irrigation.STARTING: "Starting", irrigation.RUNNING: "Running",
            irrigation.STOPPING: "Stopping: inverter stays on to settle"
            }.get(phase)
    if mode is None:
        mode = "Inverter only" if inverter.get("powered") else idle
    return Mode(mode, plan, _why(own, always=True))


def _inverter(own, pump, forced):
    plan = ("Turns on with each irrigation run, or alone when forced on. "
            "Alone, it stops when the battery falls to its limits.")
    if forced:
        return Mode("Off unless irrigation runs", plan)
    if own.get("powered") is None:
        return Mode("Mode unknown: nothing published yet", plan)
    if not own.get("powered"):
        return Mode("Off", plan, _why(own, always=True))
    if PHASES.get(_number(pump.get("phase"))) in (
            irrigation.STARTING, irrigation.RUNNING, irrigation.STOPPING):
        return Mode("On for irrigation", plan)
    return Mode("On alone for AC power", plan, _why(own, always=True))


def describe(load, row, published, readings, shedding, forced=False):
    """The lines for one relay of one load.

    `row` is the relay's name: the load's own, or its inverter's for an
    irrigation load. `published` is every load's last published entry, by
    relay name, and `shedding` is whether the shedding switch is on.

    With `forced`, the mode is what the load goes back to when the force
    ends. The published state then shows the force, not the load's own
    choice, so nothing here reads it as the load's own.
    """
    lines = _describe(load, row, published, readings, shedding, forced)
    own = _entry(published, row)
    reason = own.get("reason")
    if forced and isinstance(reason, str) and reason and not (
            reason.startswith("forced ") and reason.endswith(
                " by an operator")):
        # The controller's reason says what it did with the stored force,
        # such as refusing it, holding it back, or running under it.
        lines.why = reason
    if lines.why and lines.mode.lower().startswith(lines.why.lower()):
        lines.why = None
    return lines


def _describe(load, row, published, readings, shedding, forced):
    own = _entry(published, row)
    kind = load["type"]
    if kind == "sequenced_ac":
        if row == load["name"]:
            return _run(load, own, _entry(
                published, irrigation.inverter_name(load["name"])), forced)
        return _inverter(own, _entry(published, load["name"]), forced)
    if kind == "cpu_fan":
        return _cpu_fan(load, own, readings, forced)
    if kind == "vent_fan":
        return _vent_fan(load, own, readings, forced)
    if kind == "battery_heating":
        return _heater(load, own, readings, forced)
    return _ladder(load, own, readings, shedding)
