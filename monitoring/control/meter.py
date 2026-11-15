"""What each load draws, measured where the controller switches it.

The bank's shunt sees every load at once, so one load is measured as the
step in the bank's current across a switch of its own relay: the last shunt
reading before the switch, against the readings after it. The controller
makes the switch, so it knows the moment exactly.

A step is kept only when nothing else could have moved the current:

- no other relay switched while it was measured,
- no charge came in, from the panels or a generator, from the reading it
  starts from to the last one it ends on, and
- the reading it starts from came after any earlier switch.

A camera's own start draws more than it runs on, so its readings are taken
from two minutes after the switch. The pump is measured on its first reading
at least fifteen seconds after it starts, as the irrigation check measures
it, and the inverter that starts just before it is part of that step.

Camera branches draw more at night, when their infrared lights are on, so a
direct-current branch keeps a figure for the night and one for the day, and
a switch in twilight is not measured. The pump keeps one for moving water,
one for running dry, and one above its band. Every other relay keeps one.

The base load is what the bank draws while every switched load has been off
for ten minutes with no charge coming in: the controller, the network gear,
the radios, and any load wired around its relay.

The latest figure for each is kept on the card, so a test made once is not
lost, and published on every pass.

This module reads no clock and touches no file. The loop gives it each
pass's readings, relay states, monotonic time, wall time and sun.
"""

import datetime

#: Seconds after a switch before readings count, and the last second they
#: count, by the kind of output.
WINDOWS = {
    "pump": (15.0, 150.0),
    "inverter": (15.0, 270.0),
    "other": (120.0, 270.0),
}

#: The oldest a reading may be at the switch and still be the step's start.
BASELINE_MAX_AGE = 90.0

#: The oldest a charge reading may be and still say whether charge is coming.
CHARGE_MAX_AGE = 120.0

#: Quiet time before the base load is read, and the readings averaged.
BASE_QUIET = 600.0
BASE_READINGS = 10

#: Twilight margins around sunrise and sunset, in which a camera is not
#: measured: its infrared lights may be on or off.
NIGHT_MARGIN = datetime.timedelta(minutes=30)
DAY_MARGIN = datetime.timedelta(hours=1)


def period(local_now, suns):
    """"night", "day", or None in twilight or without a sun to place it."""
    if not suns:
        return None
    today = suns[len(suns) // 2]
    rise, set_ = today["sunrise"], today["sunset"]
    if local_now < rise - NIGHT_MARGIN or local_now > set_ + NIGHT_MARGIN:
        return "night"
    if rise + DAY_MARGIN < local_now < set_ - DAY_MARGIN:
        return "day"
    return None


def field_names(condition):
    """The published field names for a figure kept under `condition`."""
    if not condition:
        return "draw_amps", "draw_at"
    return "draw_%s_amps" % condition, "draw_%s_at" % condition


class Meter:
    """One node's load meter.

    `spec` is the node file's meter block, as spec.load_meter returns it.
    `loads` is the controller's load specs, by name. `results` is what an
    earlier process kept on the card, or None.
    """

    def __init__(self, spec, loads, results=None):
        self.spec = spec
        self.kinds = {}
        self.bands = {}
        for name, load in loads.items():
            if load["type"] == "sequenced_ac":
                self.kinds[name + ".inverter"] = "inverter"
                self.kinds[name] = "pump"
                self.bands[name] = tuple(
                    load["pump"]["readback"]["expect_amps"])
            else:
                self.kinds[name] = ("branch" if load["type"] == "dc_branch"
                                    else "other")
        self.results = _clean(results)
        #: True when results changed in a way worth writing to the card.
        self.changed = False

        self._connected = {}
        self._last_switch = None
        self._shunt = None          # (taken_at, amps) of the newest reading
        self._pending = None
        self._quiet_since = None
        self._base = []
        self._base_written = self.results.get("base", {}).get("amps")

    # -- one pass -------------------------------------------------------

    def observe(self, mono, wall, local_now, suns, readings, connected):
        """Take one pass: its readings, then the relays as they now stand.

        `connected` holds each output the loop drove this pass and whether
        its load is now running. `wall` is the epoch on a trusted clock, or
        None, and nothing is measured without one: a figure has to say when
        it was taken.
        """
        charging = self._charging(readings)
        fresh = self._new_shunt(readings)

        if self._pending is not None:
            self._advance(fresh, charging, mono, wall)

        switched = []
        for output, now_on in sorted(connected.items()):
            before = self._connected.get(output)
            self._connected[output] = now_on
            if before is None:
                # The first drive after a start: whatever the pin did, it
                # is not a step anyone saw begin.
                self._last_switch = mono
            elif before != now_on:
                switched.append((output, 1.0 if now_on else -1.0))

        if switched:
            self._switch(switched, mono, local_now, suns, charging, wall)

        self._watch_base(fresh, charging, mono, wall)

    # -- readings -------------------------------------------------------

    def _value(self, readings, source, max_age):
        reading = readings.get(source["device"]) or {}
        value = (reading.get("fields") or {}).get(source["field"])
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None, None
        if not reading.get("age_seconds", float("inf")) <= max_age:
            return None, None
        return float(value), reading.get("taken_at")

    def _charging(self, readings):
        for source in self.spec["charge"]:
            value, _ = self._value(readings, source, CHARGE_MAX_AGE)
            if value is not None and value > 0.0:
                return True
        return False

    def _new_shunt(self, readings):
        """The shunt reading if it is one not seen before, else None."""
        amps, taken = self._value(readings, self.spec["shunt"],
                                  float("inf"))
        if amps is None or taken is None:
            return None
        if self._shunt is not None and taken <= self._shunt[0]:
            return None
        self._shunt = (taken, amps)
        return self._shunt

    # -- a step ---------------------------------------------------------

    def _switch(self, switched, mono, local_now, suns, charging, wall):
        pending, self._pending = self._pending, None
        last, self._last_switch = self._last_switch, mono
        if len(switched) != 1 or charging or wall is None:
            return
        (output, direction), = switched
        kind = self.kinds.get(output)
        if kind is None or output in self.spec["wired_around"]:
            return

        if (kind == "pump" and direction > 0 and pending is not None
                and pending["output"].endswith(".inverter")
                and pending["direction"] > 0):
            # The pump follows its inverter by seconds; the step runs from
            # the reading before the inverter.
            baseline = pending["baseline"]
        else:
            baseline = self._shunt
            if baseline is None or mono - baseline[0] > BASELINE_MAX_AGE:
                return
            if last is not None and baseline[0] <= last:
                return
        if baseline[1] > 0.0:
            return

        condition = None
        if kind == "branch":
            condition = period(local_now, suns)
            if condition is None:
                return
        elif kind == "inverter":
            condition = "idle"
        self._pending = {
            "output": output, "kind": kind, "direction": direction,
            "switched": mono, "baseline": baseline, "after": [],
            "condition": condition,
        }

    def _advance(self, fresh, charging, mono, wall):
        pending = self._pending
        if charging:
            self._pending = None
            return
        opens, closes = WINDOWS.get(pending["kind"], WINDOWS["other"])
        if fresh is not None:
            age = fresh[0] - pending["switched"]
            if opens <= age <= closes:
                if fresh[1] > 0.0:
                    self._pending = None
                    return
                pending["after"].append(fresh[1])
        first_only = pending["kind"] == "pump"
        if (first_only and pending["after"]) or mono - pending["switched"] > closes:
            self._pending = None
            if pending["after"] and wall is not None:
                self._finish(pending, wall)

    def _finish(self, pending, wall):
        after = pending["after"][:1] if pending["kind"] == "pump" \
            else pending["after"]
        amps = (pending["baseline"][1] - sum(after) / len(after)) \
            * pending["direction"]
        condition = pending["condition"]
        if pending["kind"] == "pump":
            low, high = self.bands[pending["output"]]
            condition = ("dry" if amps < low else "high" if amps > high
                         else "wet")
        figures = self.results.setdefault(pending["output"], {})
        figures[condition or ""] = {"amps": round(amps, 3), "at": wall}
        self.changed = True

    # -- the base load --------------------------------------------------

    def _watch_base(self, fresh, charging, mono, wall):
        running = any(on for output, on in self._connected.items()
                      if output not in self.spec["wired_around"])
        if running or charging or self._quiet_since is None:
            self._quiet_since = None if (running or charging) else mono
            self._base = []
            return
        if fresh is None or fresh[0] - self._quiet_since < BASE_QUIET:
            return
        if fresh[1] > 0.0:
            self._base = []
            return
        self._base = (self._base + [-fresh[1]])[-BASE_READINGS:]
        if len(self._base) < BASE_READINGS or wall is None:
            return
        amps = round(sum(self._base) / len(self._base), 3)
        self.results["base"] = {"amps": amps, "at": wall}
        # Written when it moves, so a quiet night is a few writes, not one
        # a minute.
        if (self._base_written is None
                or abs(amps - self._base_written) >= 0.05):
            self._base_written = amps
            self.changed = True

    # -- what it reports ------------------------------------------------

    def fields_for(self, output):
        """The published fields for one output's figures."""
        out = {}
        for condition, figure in sorted(
                (self.results.get(output) or {}).items()):
            amps_key, at_key = field_names(condition)
            out[amps_key] = float(figure["amps"])
            out[at_key] = float(figure["at"])
        return out

    def base_fields(self):
        figure = self.results.get("base")
        if not figure:
            return {}
        return {"base_amps": float(figure["amps"]),
                "base_at": float(figure["at"])}


def _clean(results):
    """Only well-formed figures from a record written by an earlier process."""
    out = {}
    if not isinstance(results, dict):
        return out
    for output, figures in results.items():
        if output == "base":
            if _figure(figures):
                out["base"] = dict(figures)
            continue
        if not isinstance(figures, dict):
            continue
        kept = {c: dict(f) for c, f in figures.items()
                if isinstance(c, str) and _figure(f)}
        if kept:
            out[output] = kept
    return out


def _figure(value):
    return (isinstance(value, dict)
            and all(isinstance(value.get(k), (int, float))
                    and not isinstance(value.get(k), bool)
                    for k in ("amps", "at")))
