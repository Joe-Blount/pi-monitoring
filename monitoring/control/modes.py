"""Plain words for what each load is set to do, for the override page.

Each load gets two lines: its mode, which says what it is doing and why in a
few words, and its plan, which says what the node file tells it to do in
every case. A person reading the page on a phone sees from the mode whether
the load is where they expect it, and from the plan what comes next.

Nothing here decides anything. The words come from the node file and from
what the loop last published, so they can never disagree with the loop.
"""

from monitoring.control import decide, irrigation

#: The phase an irrigation load publishes, as a number, back to its name.
PHASES = {code: name for name, code in irrigation.PHASE_CODES.items()}


def _clock(minutes):
    return "%02d:%02d" % divmod(int(minutes) % 1440, 60)


def _covers(schedule, minute):
    start, end = schedule["on_from"], schedule["on_to"]
    if start < end:
        return start <= minute < end
    return minute >= start or minute < end


def _offset(edge):
    event, delta = edge
    seconds = int(delta.total_seconds())
    if seconds == 0:
        return "at %s" % event
    hours, rest = divmod(abs(seconds), 3600)
    if rest:
        amount = "%d min" % (abs(seconds) // 60)
    else:
        amount = "%d h" % hours
    return "%s %s %s" % (amount, "before" if seconds < 0 else "after", event)


def schedule_mode(schedule):
    """The mode line for a battery step's schedule."""
    kind = schedule["type"]
    if kind == "never":
        return "Off to save battery"
    if kind == "clock":
        hours = "on %s to %s" % (_clock(schedule["on_from"]),
                                 _clock(schedule["on_to"]))
        noon, midnight = _covers(schedule, 720), _covers(schedule, 0)
        if noon and not midnight:
            return "Day only (%s, off at night)" % hours
        if midnight and not noon:
            return "Night only (%s, off by day)" % hours
        return "Set hours only (%s)" % hours
    windows = ", and ".join("from %s to %s" % (_offset(w["from"]),
                                               _offset(w["to"]))
                            for w in schedule["on_windows"])
    return "%s (on %s)" % (_solar_name(schedule).capitalize(), windows)


def _solar_name(schedule):
    events = {edge[0] for window in schedule["on_windows"]
              for edge in (window["from"], window["to"])}
    if events == {"sunrise", "sunset"}:
        return "dawn and dusk only"
    if events == {"sunrise"}:
        return "mornings only"
    if events == {"sunset"}:
        return "evenings only"
    return "set solar hours only"


def _schedule_short(schedule):
    kind = schedule["type"]
    if kind == "never":
        return "off"
    if kind == "clock":
        noon, midnight = _covers(schedule, 720), _covers(schedule, 0)
        if noon and not midnight:
            return "day only"
        if midnight and not noon:
            return "night only"
        return "on %s to %s" % (_clock(schedule["on_from"]),
                                _clock(schedule["on_to"]))
    return _solar_name(schedule).replace(" only", "")


def _reading(readings, source):
    if not isinstance(source, dict):
        return None
    value = ((readings.get(source.get("device")) or {}).get("fields") or {}
             ).get(source.get("field"))
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value


def _temperature(value):
    return "no reading" if value is None else "%.0f F" % value


def _ladder(load, published, readings, shedding):
    ladder = load["shed_ladder"]
    tiers = ladder["tiers"]
    battery = _reading(readings, ladder["source"])
    steps = " ".join("Below %g%%: %s." % (tier["below_pct"],
                                         _schedule_short(tier["schedule"]))
                     for tier in tiers)
    plan = ("Battery %s. %s" % ("no reading" if battery is None
                                 else "%.0f%%" % battery, steps))
    code = published.get("reason_code")
    level = published.get("shed_level")
    if code == decide.FLOOR:
        return "Off: battery at its floor", plan
    if not shedding:
        return "Always on: shedding paused", plan
    if isinstance(level, bool) or not isinstance(level, (int, float)):
        return "Mode unknown: nothing published yet", plan
    depth = int(level)
    if depth <= 0:
        return "Always on", plan
    mode = schedule_mode(tiers[min(depth, len(tiers)) - 1]["schedule"])
    if code == decide.SCHEDULE_SUSPENDED:
        mode += ". Held on: the clock is not trusted"
    return mode, plan


def _fan_state(published, what):
    powered = published.get("powered")
    if powered is None:
        return "state unknown"
    return what if powered else "idle"


def _cpu_fan(load, published, readings):
    value = _reading(readings, load["source"])
    mode = "By temperature, %s, %s" % (_fan_state(published, "running"),
                                       _temperature(value))
    plan = "Runs above %g F, stops below %g F." % (load["run_above_f"],
                                                    load["stop_below_f"])
    return mode, plan


def _vent_fan(load, published, readings):
    inside = _reading(readings, load["inside"])
    outside = _reading(readings, load["outside"])
    mode = "By temperature, %s, inside %s, outside %s" % (
        _fan_state(published, "running"), _temperature(inside),
        _temperature(outside))
    plan = ("Runs when inside is above %g F and outside is %g F cooler. "
            "Stops when inside is below %g F or outside is less than %g F "
            "cooler." % (load["inside_above_f"], load["outside_cooler_by_f"],
                         load["inside_below_f"],
                         load["outside_cooler_by_less_than_f"]))
    if load.get("outside_below_f") is not None:
        plan += " Stops when outside is below %g F." % load["outside_below_f"]
    return mode, plan


def _heater(load, published, readings):
    values = [_reading(readings, source) for source in load["sources"]]
    known = [value for value in values if value is not None]
    coldest = min(known) if known else None
    mode = "By temperature, %s, coldest %s" % (
        _fan_state(published, "heating"), _temperature(coldest))
    plan = "Heats below %g F, stops above %g F." % (
        load["coldest_below_f"], load["coldest_above_f"])
    return mode, plan


def _run(load, published, inverter):
    if load.get("schedule"):
        idle = "Idle: waiting for the next scheduled day"
        plan = "Runs every %d days." % load["schedule"]["every_days"]
    else:
        idle = "Idle: manual only"
        plan = "Runs only when started here."
    plan += " A run stops at every safety limit: a dry or stalled pump, the "\
            "battery, the run limit."
    if published.get("locked_out"):
        return "Locked out: too many dry starts", plan
    phase = PHASES.get(published.get("phase"))
    if phase is None:
        return "Mode unknown: nothing published yet", plan
    if phase == irrigation.STARTING:
        return "Starting", plan
    if phase == irrigation.RUNNING:
        return "Running", plan
    if phase == irrigation.STOPPING:
        return "Stopping: inverter stays on to settle", plan
    if (inverter or {}).get("powered"):
        return "Inverter only", plan
    return idle, plan


def _inverter(published, pump):
    plan = ("Turns on with each irrigation run, or alone when forced on. "
            "Alone, it stops at the battery floor.")
    if published.get("powered") is None:
        return "Mode unknown: nothing published yet", plan
    if not published.get("powered"):
        return "Off", plan
    phase = PHASES.get((pump or {}).get("phase"))
    if phase in (irrigation.STARTING, irrigation.RUNNING,
                 irrigation.STOPPING):
        return "On for irrigation", plan
    return "On alone for AC power", plan


def describe(load, row, published, readings, shedding):
    """The mode and plan lines for one relay of one load.

    `row` is the relay's name: the load's own, or its inverter's for an
    irrigation load. `published` is every load's last published entry, by
    relay name, and `shedding` is whether the shedding switch is on.
    """
    own = published.get(row) or {}
    kind = load["type"]
    if kind == "sequenced_ac":
        if row == load["name"]:
            return _run(load, own, published.get(irrigation.inverter_name(
                load["name"])))
        return _inverter(own, published.get(load["name"]))
    if kind == "cpu_fan":
        return _cpu_fan(load, own, readings)
    if kind == "vent_fan":
        return _vent_fan(load, own, readings)
    if kind == "battery_heating":
        return _heater(load, own, readings)
    return _ladder(load, own, readings, shedding)
