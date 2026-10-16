"""What each load should be doing, as a function of what was measured.

No input and no output. Readings, the time, the switches and the previous
state go in; a decision per load and the next state come out. Everything that
is awkward to get right lives here, where a test can hold the clock still and
walk a battery down through every threshold in a few microseconds, rather than
in the loop that drives relays.

The ladder
----------
Each sheddable load carries its own ladder, so a second group of cameras with
different thresholds is a config addition rather than a change to this file.

A tier latches when its source has been below the tier's entry for a number of
consecutive readings, and unlatches when the source rises strictly above entry
plus the release margin. Entry is counted in readings rather than timed,
because the battery arrives every few minutes from the slow collector and a
dwell measured in seconds would be satisfied by a single sample.

**The load runs only while every latched tier permits it.** Not "the deepest
latched tier decides". With intersection, a deeper tier can only ever remove
running time, so the ladder cannot command more power at a lower state of
charge however the windows are later edited. Under
deepest-tier-decides, a window reaching past a shallower tier's permitted
period would do exactly that, and the only defence would be arithmetic about
solstices run against a config file.

Because tiers latch independently, the latched set is always a prefix of the
ladder: a deeper tier cannot be latched while a shallower one is not. That
holds only because the tiers are strictly descending and share one release
margin, which the spec enforces.

The floor
---------
A cell voltage below the floor sheds the load regardless of tier, schedule,
clock, override, the shedding switch, or any minimum run time. It is the one
rule with nothing above it, because a cell under its floor is not a judgment
call about convenience. It needs no clock, so it keeps working on a machine
that has no idea what day it is.

The floor takes a second opinion where one exists. The battery reading comes
over Bluetooth from a device that is routinely quiet for minutes, and a ladder
with one source hangs entirely on the least reliable thing on the node. Where
a second, independently wired source is declared, either can shed.

When the clock cannot be trusted
--------------------------------
Tiers whose schedule needs to know the time are suspended; tiers that are
simply "never run" still apply, and so does the floor. The resulting defaults
fall out of the wiring rather than from special cases here: the cameras are
wired so that de-energized means running, and irrigation so that de-energized
means off.

An override cannot be allowed to expire against a clock that may be wrong, so
while the clock is untrusted a force-off is honoured and a force-on is not.
Ignoring force-on costs nothing, because a load whose default is to run is
already running.
"""

import collections

#: What a load is told to do, and why. The reason travels with the decision
#: because it is published: somebody looking at a dark camera deserves to see
#: which number turned it off.
Decision = collections.namedtuple(
    "Decision", "load connected tier reason_code reason decided_on")

#: Reason codes, published as numbers so an alert can match without parsing
#: text. Text says which number; the code says which kind of rule.
NONE = 0
STATE_OF_CHARGE = 1
FLOOR = 2
HOLDING_STALE = 3
OVERRIDE = 4
SHEDDING_DISABLED = 5
SCHEDULE_SUSPENDED = 6
NO_READING = 7
THERMAL = 8
REFUSED = 9

#: A decision the rules made and a timer has not let through yet. Its own
#: code because without it the published code describes the decision while
#: the relay sits in the opposite state: an alert matching "nothing is wrong"
#: would see a shed load, and one matching "shedding on state of charge"
#: would see a powered one. Only the free text explained it.
HELD_BY_TIMER = 10


def _reading(readings, source, stale_after):
    """One field's value, or None when it cannot be trusted.

    A field missing from an otherwise fresh reading is not a zero and not a
    false. It is an absence, and a term with no reading is dropped from the
    test it appears in rather than counted against the load.
    """
    if not source:
        return None
    record = readings.get(source["device"])
    if not record:
        return None
    if record.get("age_seconds", float("inf")) > stale_after:
        return None
    value = record.get("fields", {}).get(source["field"])
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    # Not a number is not a reading. Every comparison against a NaN is
    # false, so one arriving here would look like a value clear of every
    # bound and would release a latched floor.
    if value != value or value in (float("inf"), float("-inf")):
        return None
    return value


# -- the ladder -------------------------------------------------------------

def _source_key(source):
    return "%s.%s" % (source["device"], source["field"])


def _step_floor(ladder, readings, was_latched, latched_by):
    """Whether the floor is tripped, on what, and which source owns it.

    Each source carries its own bounds, because the sources are not in the
    same units. One is a cell voltage and the other a pack voltage, and a
    shared pair of bounds compares a healthy cell at 3.30 against a pack
    threshold of 12.6 and sheds forever.

    **Any source may latch the floor. Only the source that latched it may
    release it.** That asymmetry is the whole point of having two, and
    getting it wrong is self-reinforcing rather than merely wrong.

    A pack voltage cannot see one weak cell: a cell at 3.10 beside three at
    3.43 reads as a 13.39 V pack, comfortably clear of any pack bound. So if
    either source could release, a cell-tripped floor would be cleared by the
    pack the moment the battery radio went quiet, which it does routinely.
    And shedding this load removes most of the draw, so the pack voltage
    rises as a direct result of the shed. The floor would release, the load
    would reconnect onto the weak cell, the cell would sag, and the cycle
    would repeat for as long as the radio stayed quiet.
    """
    floor = ladder.get("floor")
    if not floor:
        return False, None, None, None

    stale_after = ladder["stale_after"]
    seen = {}
    for source in floor["sources"]:
        seen[_source_key(source)] = (source,
                                     _reading(readings, source, stale_after))

    # Entry: any source below its own bound, and it becomes the source that
    # has to clear it.
    for key, (source, value) in seen.items():
        if value is not None and value < source["shed_below"]:
            return True, value, source, key

    if not was_latched:
        return False, None, None, None

    owner = seen.get(latched_by)
    if owner is None:
        # The source that latched it is no longer declared at all, so nothing
        # can speak for it. Holding is the only honest answer.
        return True, None, None, latched_by

    source, value = owner
    if value is None:
        # The source that latched it has gone quiet. A floor holds until a
        # reading clears it, with no time limit, because the alternative is
        # releasing a shed on the strength of a radio going silent.
        return True, None, None, latched_by
    if value <= source["restore_above"]:
        # Readable, above its shedding bound, not yet clear of its restoring
        # one. Still latched, and on a live reading rather than a guess.
        return True, value, source, latched_by
    return False, None, None, None


def _step_tiers(ladder, value, mono_now, latched, below_counts, above_since):
    """Advance each tier's latch independently.

    `value` is None when the source is not readable, in which case every
    latch is held exactly as it was and every counter is cleared: the battery
    judgment has become unknowable, so it stops moving rather than drifting
    in either direction. Clearing the counters means a reading either side of
    a stale gap does not count as two in a row, which it is not: consecutive
    has to mean consecutive or the requirement is no requirement at all.

    Entry is counted in readings and release is timed. They are deliberately
    different. Entry guards against one glitched sample latching a tier whose
    release is fifteen points away, and a count does that whatever rate the
    sensor arrives at. Release guards against restoring on a charging spike,
    which is a thing that lasts seconds regardless of how many readings land
    inside it, so it is held off by the clock.
    """
    latched = dict(latched)
    counts = dict(below_counts)
    since = dict(above_since)
    if value is None:
        return latched, {}, {}

    needed = ladder["enter_after_readings"]
    margin = ladder["release_margin_pct"]
    dwell = ladder["restore_dwell"]
    for index, tier in enumerate(ladder["tiers"]):
        key = str(index)
        entry = tier["below_pct"]
        if value < entry:
            counts[key] = counts.get(key, 0) + 1
            since.pop(key, None)
            if counts[key] >= needed:
                latched[key] = True
        else:
            counts[key] = 0
            if value > entry + margin:
                first = since.setdefault(key, mono_now)
                if mono_now - first >= dwell:
                    latched[key] = False
            else:
                since.pop(key, None)
    return latched, counts, since


def _permits(tier, now, suns, clock_trusted):
    """Whether one latched tier allows the load to run now.

    Returns None when the tier cannot be judged, which happens only when its
    schedule needs a clock and the clock is not trustworthy. The caller then
    drops the tier from the intersection rather than guessing either way.
    """
    schedule = tier["schedule"]
    kind = schedule["type"]

    if kind == "never":
        return False
    if not clock_trusted:
        return None
    if kind == "clock":
        start, end = schedule["on_from"], schedule["on_to"]
        minute = now.hour * 60 + now.minute
        if start <= end:
            return start <= minute < end
        # Wraps midnight, so the permitted period is the union of the two
        # ends of the day rather than the gap between them.
        return minute >= start or minute < end
    if kind == "solar":
        # Every window is tried against each day's events, not just today's.
        # A window is an offset from an event, so one reaching far enough
        # past sunset belongs partly to the next calendar day, and the hour
        # after midnight is inside a window anchored on yesterday. Checking
        # today alone silently clips that tail, and the shipped offset of one
        # hour is only one edit away from reaching it.
        for day in suns:
            for window in schedule["on_windows"]:
                begin = day[window["from"][0]] + window["from"][1]
                finish = day[window["to"][0]] + window["to"][1]
                if begin <= now < finish:
                    return True
        return False
    raise AssertionError("unknown schedule type %r" % kind)


def ladder_decision(name, spec, readings, now, mono_now, suns, clock_trusted,
                    state):
    """Decide one sheddable load. Returns (Decision, new state)."""
    ladder = spec["shed_ladder"]
    was = state.get("floor_latched", False)
    floor_latched, floor_value, floor_source, floor_owner = _step_floor(
        ladder, readings, was, state.get("floor_latched_by"))

    value = _reading(readings, ladder["source"], ladder["stale_after"])
    latched, counts, since = _step_tiers(
        ladder, value, mono_now, state.get("latched", {}),
        state.get("below", {}), state.get("above_since", {}))

    new_state = dict(state)
    new_state["latched"] = latched
    new_state["below"] = counts
    new_state["above_since"] = since
    new_state["floor_latched"] = floor_latched
    new_state["floor_latched_by"] = floor_owner

    if floor_latched:
        if floor_value is None:
            reason = ("holding the floor with nothing readable to clear it")
        elif floor_value < floor_source["shed_below"]:
            reason = "%s=%.3f below the %.3f floor" % (
                floor_source["field"], floor_value,
                floor_source["shed_below"])
        else:
            # Between the two bounds. Saying it is below the shedding bound
            # here would assert a comparison that is false, and an operator
            # checking it against the dashboard would doubt the sensor.
            reason = "%s=%.3f has not cleared the %.3f restoring bound" % (
                floor_source["field"], floor_value,
                floor_source["restore_above"])
        return (Decision(name, False, _depth(latched), FLOOR, reason,
                         floor_value),
                new_state)

    active = [tier for index, tier in enumerate(ladder["tiers"])
              if latched.get(str(index))]
    if not active:
        reason = ("%s=%.1f above every threshold" % (
            ladder["source"]["field"], value) if value is not None
            else "no threshold latched")
        return (Decision(name, True, 0, NONE if value is not None
                         else HOLDING_STALE, reason, value),
                new_state)

    suspended = False
    for tier in active:
        verdict = _permits(tier, now, suns, clock_trusted)
        if verdict is None:
            suspended = True
            continue
        if not verdict:
            where = "below %d%%" % tier["below_pct"]
            if value is None:
                reason = "holding %s on a stale reading" % where
                code = HOLDING_STALE
            else:
                reason = "%s=%.1f %s" % (
                    ladder["source"]["field"], value, where)
                code = STATE_OF_CHARGE
            return (Decision(name, False, _depth(latched), code, reason,
                             value),
                    new_state)

    if suspended:
        return (Decision(name, True, _depth(latched), SCHEDULE_SUSPENDED,
                         "a schedule is latched but the clock is not "
                         "trustworthy, so it is not applied", value),
                new_state)

    code = HOLDING_STALE if value is None else STATE_OF_CHARGE
    return (Decision(name, True, _depth(latched), code,
                     "every latched schedule permits it now", value),
            new_state)


def _depth(latched):
    """How many tiers are latched, which is the published shed level."""
    return sum(1 for value in latched.values() if value)


# -- the switches an operator holds -----------------------------------------

def _override(name, switches, now, clock_trusted):
    """An operator's force for this load, or None.

    Expired overrides are gone. An override with no expiry would be the one
    somebody set during a hunt and forgot, so the CLI refuses to write one,
    and an absent expiry here is treated as expired rather than as forever.

    While the clock is untrusted a force-off still applies and a force-on does
    not. The asymmetry is deliberate and both halves are in the safe
    direction: an expiry cannot be judged against a clock that may be wrong,
    and the load whose default is to run is already running.
    """
    entry = (switches.get("loads") or {}).get(name)
    if not isinstance(entry, dict):
        return None
    force = entry.get("force")
    if force not in ("on", "off"):
        return None
    expires = entry.get("expires_epoch")
    if not isinstance(expires, (int, float)) or isinstance(expires, bool):
        return None
    if not clock_trusted:
        return "off" if force == "off" else None
    if now.timestamp() >= float(expires):
        return None
    return force


def _apply_switches(decision, name, switches, now, clock_trusted):
    """Let an operator's switches override a state-of-charge decision.

    Never reached when the floor has fired. A floor an override can defeat is
    not a floor, and the number it defends is the one standing between a
    forgotten command and a damaged cell.
    """
    force = _override(name, switches, now, clock_trusted)
    if force is not None:
        return decision._replace(
            connected=(force == "on"), reason_code=OVERRIDE,
            reason="forced %s by an operator" % force)

    if not switches.get("shedding_enabled", True) and not decision.connected:
        return decision._replace(
            connected=True, reason_code=SHEDDING_DISABLED,
            reason="would shed (%s) but shedding is switched off"
                   % decision.reason)
    return decision


# -- the timers -------------------------------------------------------------

def _hold(decision, spec, mono_now, state):
    """Apply the minimum run and minimum off times.

    A recorder should not be cycled repeatedly, so a change of mind inside
    those windows waits. The floor is exempt: a minimum run time that held a
    load up while a cell sat under its floor would defeat the one rule with
    nothing above it.
    """
    commanded = state.get("commanded")
    since = state.get("since")
    new_state = dict(state)

    if decision.reason_code == FLOOR or commanded is None or not isinstance(
            since, (int, float)):
        new_state["commanded"] = decision.connected
        if commanded is not decision.connected:
            new_state["since"] = mono_now
        return decision, new_state

    if decision.connected == commanded:
        return decision, new_state

    held = mono_now - float(since)
    wanted = spec["min_run"] if commanded else spec["min_off"]
    if held < wanted:
        return (decision._replace(
            connected=commanded, reason_code=HELD_BY_TIMER,
            reason="%s, but held for another %.0fs by the minimum %s time"
                   % (decision.reason, wanted - held,
                      "run" if commanded else "off")),
                new_state)

    new_state["commanded"] = decision.connected
    new_state["since"] = mono_now
    return decision, new_state


# -- the whole node ---------------------------------------------------------

#: Load types this decides. A declared type that is not here is refused
#: rather than ignored, because a load nobody drives and nobody mentions is
#: indistinguishable from one that is working.
LADDER_TYPES = ("dc_branch",)


def decide(local_now, mono_now, clock_trusted, readings, switches,
           specs, state, suns):
    """Decide every load on a node. Returns (decisions, new state).

    Pure, and takes two clocks because they answer different questions.

    `local_now` is a timezone-aware datetime. The schedules compare against
    it, and an override's expiry comes from its own timestamp, so there is one
    source for both and no way to pass a pair that disagree.

    `suns` is a sequence of per-day event mappings covering the day before,
    the day itself and the day after, because a solar window can reach across
    midnight in either direction.

    `mono_now` drives every age and every timer, because the wall clock steps
    by hours the first time it synchronises after a restored time, and every
    dwell would expire together.
    """
    decisions = {}
    new_state = {}

    for name in sorted(specs):
        spec = specs[name]
        prior = state.get(name) or {}

        if spec["type"] in LADDER_TYPES:
            decision, load_state = ladder_decision(
                name, spec, readings, local_now, mono_now, suns,
                clock_trusted, prior)
            if decision.reason_code != FLOOR:
                decision = _apply_switches(
                    decision, name, switches, local_now, clock_trusted)
        else:
            decision = Decision(
                name, False, 0, REFUSED,
                "load type %r is declared but this controller does not drive "
                "it" % spec["type"], None)
            load_state = dict(prior)

        decision, load_state = _hold(decision, spec, mono_now, load_state)
        decisions[name] = decision
        new_state[name] = load_state

    return decisions, new_state

