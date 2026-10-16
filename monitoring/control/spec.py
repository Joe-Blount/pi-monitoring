"""Reading a control block, and refusing one that cannot be trusted.

The node file is edited by hand, on a laptop, about a machine an hour away,
and often while standing next to different hardware. So everything here
refuses rather than defaults. A control block that is wrong in a way this
cannot see is a relay doing the opposite of what somebody meant, discovered
weeks later from a gap in the data.

The rule throughout: a key nobody recognises is an error, not a comment. A
typo in a threshold name silently leaves the default in place, the load
behaves as though the line was never written, and the file reads as though it
was. That is the same failure as a load type that is not in the registry,
which this project has already been bitten by: the type yields no loads, and
every test about that load passes by finding nothing to check.
"""

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

LOAD_KEYS = {"type", "enabled", "control_pin", "polarity", "on_stale_data",
             "shed_ladder", "min_run", "min_off", "readback"}
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


def _only(where, mapping, allowed):
    if not isinstance(mapping, dict):
        _fail(where, "expected a mapping, found %s" % type(mapping).__name__)
    unknown = set(mapping) - allowed
    if unknown:
        _fail(where, "unknown key(s) %s; a typo here leaves the default in "
                     "place and the file reads as though it did not"
                     % ", ".join(sorted(unknown)))


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
        total = int(hours) * 60 + int(minutes)
    except ValueError:
        _fail(where, "%s is not a time: %r" % (what, value))
    if not 0 <= total <= 24 * 60:
        _fail(where, "%s is not a time of day: %r" % (what, value))
    return total


def _duration(where, value, what):
    try:
        return _seconds(value, what)
    except DurationError as exc:
        _fail(where, str(exc))


def _source(where, raw, devices):
    _only(where, raw, SOURCE_KEYS)
    for key in ("device", "field"):
        if not isinstance(raw.get(key), str) or not raw[key]:
            _fail(where, "needs a %s" % key)
    if raw["device"] not in devices:
        _fail(where, "names device %r, which this node does not declare. "
                     "Known: %s" % (raw["device"], ", ".join(sorted(devices))))
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
            edges[edge] = (event, _duration(at, offset, "%s offset" % edge))
        out.append(edges)
    return {"type": "solar", "on_windows": out}


def _ladder(where, raw, devices):
    _only(where, raw, LADDER_KEYS)
    for key in ("source", "stale_after", "release_margin_pct", "tiers"):
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
        "stale_after": _duration(where, raw["stale_after"], "stale_after"),
        "release_margin_pct": margin,
        "enter_after_readings": entered,
        "restore_dwell": _duration(where, raw.get("restore_dwell", 0),
                                   "restore_dwell"),
        "floor": (_floor(where + " floor", raw["floor"], devices)
                  if raw.get("floor") else None),
        "tiers": out_tiers,
    }


def load_spec(name, raw, devices):
    """Parse one load. Returns the spec a decision can act on."""
    where = "load %s" % name
    _only(where, raw, LOAD_KEYS)

    kind = raw.get("type")
    if kind in DEFERRED_TYPES:
        if raw.get("enabled", True):
            _fail(where, "type %r is specified but not driven yet, so it must "
                         "be enabled: false with the reason in a comment. An "
                         "enabled load nothing drives is indistinguishable "
                         "from one that works" % kind)
        return None
    if kind not in DRIVEN_TYPES:
        _fail(where, "type %r is not a type this controller drives; known: %s"
                     % (kind, ", ".join(DRIVEN_TYPES + DEFERRED_TYPES)))

    if not raw.get("enabled", True):
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
    devices = {device.name for device in node.devices}
    loads = {}
    for name in sorted((node.control.get("loads") or {})):
        spec = load_spec(name, node.control["loads"][name], devices)
        if spec is not None:
            loads[name] = spec
    return loads
