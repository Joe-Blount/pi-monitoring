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
DRIVEN_TYPES = ("dc_branch",)

#: Declared, specified, and deliberately not driven yet. An enabled load of
#: this type is refused: its thresholds are placeholders pending measurement,
#: and it is the only load here that can flood a field.
DEFERRED_TYPES = ("sequenced_ac",)

SCHEDULE_TYPES = ("clock", "solar", "never")

#: Events a solar window may be measured from.
SOLAR_EVENTS = ("sunrise", "sunset")

#: Keys a driven load may carry. Deliberately only the ones something reads.
#:
#: `readback` and `on_stale_data` are specified in the node files and nothing
#: acts on either yet, so they are absent here and a load carrying one is
#: refused. Accepting a key nobody reads is the exact failure this module
#: exists to prevent: the line sits in the file looking like it is in force.
#: They go back in alongside the code that honours them.
LOAD_KEYS = {"type", "enabled", "control_pin", "polarity",
             "shed_ladder", "min_run", "min_off"}

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
CONTROL_KEYS = {"enabled", "loads", "loop_interval", "clock"}

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
}
LADDER_KEYS = {"source", "stale_after", "release_margin_pct",
               "enter_after_readings", "restore_dwell", "floor", "tiers"}
FLOOR_KEYS = {"sources"}
FLOOR_SOURCE_KEYS = {"device", "field", "shed_below", "restore_above"}
SOURCE_KEYS = {"device", "field"}
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
    return float(value)


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
                     "ever write a reading for it, so no tier would latch and "
                     "the controller would run and do nothing. Known: %s"
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

    _only(where, raw, LOAD_KEYS)
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

    if "shed_ladder" not in raw:
        _fail(where, "needs a shed_ladder")

    return {
        "name": name,
        "type": kind,
        "control_pin": pin,
        "polarity": polarity,
        "min_run": _duration(where, raw.get("min_run", 0), "min_run"),
        "min_off": _duration(where, raw.get("min_off", 0), "min_off"),
        "shed_ladder": _ladder(where + " shed_ladder", raw["shed_ladder"],
                               devices),
    }


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
    return loads
