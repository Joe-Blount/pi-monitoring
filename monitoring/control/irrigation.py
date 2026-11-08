"""The irrigation load: an inverter and a pump, switched in order.

Two relays make one load. The pump runs only through the inverter, so the
pump is never on while the inverter is off:

- start: inverter on, then the pump once the inverter has settled
- stop: pump off, then the inverter once the pump has spun down

Each step takes at least one pass of the loop, so the order holds whatever
the loop interval is.

Two things can turn the inverter on. A run turns it on for the pump. An
operator's force on the inverter alone turns it on for general AC power, and
the pump stays off. Either way the battery floor turns it off.

A run starts from an operator's request (a force on the load, which ends when
the force expires) or from the schedule. Every run must pass the start
conditions, and every run stops at the first of the safety stops. No force
gets past a safety stop.

The dry-run check compares the battery current before the pump started with
the first shunt reading taken a settle time after it. A rise below the pump's
expected range means a dry pump or a lost prime. A rise above it means a
stalled pump. Either one stops the run and counts as a failed start, and
enough failed starts in one day lock irrigation out until the next day.

What must outlive a reboot (the day's amp-hours, the failed starts, the
lockout, the last day watered, the requests already used) is kept in the
"persist" part of the state, and the loop writes it to the card when it
changes. That is a few writes a day.
"""

import datetime

from monitoring.control import decide

#: The relay that turns on the inverter has this suffix on the load's name. It
#: is also the name of the operator's inverter-only force.
INVERTER = ".inverter"

#: The switches entry an operator writes to end a lockout.
CLEAR_LOCKOUT = ".clear_lockout"

IDLE, STARTING, RUNNING, STOPPING = "idle", "starting", "running", "stopping"
PHASE_CODES = {IDLE: 0.0, STARTING: 1.0, RUNNING: 2.0, STOPPING: 3.0}

#: How many used requests are remembered. Enough for a day of manual runs.
REMEMBERED_REQUESTS = 20


def inverter_name(name):
    return name + INVERTER


# -- the state that outlives a reboot -----------------------------------------

def _persist_for_today(persist, today, clock_trusted):
    """The record, with the daily counts reset on a new local day.

    Reset only on a trusted clock, and only forward. A clock that comes up
    on yesterday's date and is then corrected would otherwise reset the
    day's amp-hours twice, and the daily limit with them.
    """
    out = dict(persist or {})
    held = out.get("day")
    if clock_trusted and (not isinstance(held, str) or today > held):
        out["day"] = today
        out["amp_hours_today"] = 0.0
        out["failed_today"] = 0
        out["skipped_today"] = []
    out.setdefault("day", None)
    out.setdefault("amp_hours_today", 0.0)
    out.setdefault("failed_today", 0)
    out.setdefault("skipped_today", [])
    out.setdefault("consumed", [])
    return out


def _locked_out(persist):
    return persist.get("day") is not None and (
        persist.get("lockout_day") == persist["day"])


# -- readings -----------------------------------------------------------------

def _value(readings, spec, key, stale_after=None):
    source = spec["sources"].get(key)
    limit = spec["stale_after"] if stale_after is None else stale_after
    return decide._reading(readings, source, limit)


def _shunt(readings, spec):
    """The shunt current and its stamp, or (None, None) when stale."""
    value = _value(readings, spec, "shunt", spec["shunt_stale_after"])
    if value is None:
        return None, None
    return value, decide._stamp(readings, spec["sources"]["shunt"])


def _battery_fails(soc, volts, soc_min, volts_min):
    """The reason the battery is too low, or None when it is high enough."""
    if soc is None:
        return "no fresh state of charge reading"
    if volts is None:
        return "no fresh battery voltage reading"
    if soc <= soc_min:
        return "state of charge %.1f%% is not above %g%%" % (soc, soc_min)
    if volts <= volts_min:
        return "battery %.2f V is not above %g V" % (volts, volts_min)
    return None


def _keep_fails(spec, readings, floor_reason):
    """The reason a running inverter or pump must stop, or None."""
    if floor_reason:
        return floor_reason
    keep = spec["continue"]
    return _battery_fails(_value(readings, spec, "soc"),
                          _value(readings, spec, "volts"),
                          keep["soc_above"], keep["volts_above"])


# -- requests -----------------------------------------------------------------

def _force(name, switches, local_now, clock_trusted):
    return decide._override(name, switches, local_now, clock_trusted)


def _manual_request(name, switches, local_now, clock_trusted):
    """The operator's run request: its identity and its end, or None."""
    if _force(name, switches, local_now, clock_trusted) != "on":
        return None
    expires = float(switches["loads"][name]["expires_epoch"])
    return {"id": "manual:%r" % expires, "kind": "manual",
            "ends_epoch": expires}


def _start_time(entry, local_now, suns):
    """When a scheduled start falls today, as an aware datetime."""
    at = entry["at"]
    if at[0] == "clock":
        midnight = local_now.replace(hour=0, minute=0, second=0,
                                     microsecond=0)
        return midnight + datetime.timedelta(minutes=at[1])
    today = suns[1] if len(suns) > 1 else None
    if today is None:
        return None
    return today[at[1]] + at[2]


def _scheduled_request(spec, persist, local_now, suns):
    """A scheduled start that is due now, or None.

    A watering day is one every_days or more after the last day watered, or
    the day the schedule already chose, so a second start on that day still
    runs after the first. A start is due from its time to the end of the day,
    so a pass that comes late still runs it, and the run lasts its full
    length from when it starts.
    """
    schedule = spec["schedule"]
    if not schedule:
        return None
    today = local_now.date()
    last = persist.get("last_run_day")
    chosen = persist.get("schedule_day") == today.isoformat()
    if last and not chosen:
        days = (today - datetime.date.fromisoformat(last)).days
        if days < schedule["every_days"]:
            return None
    for index, entry in enumerate(schedule["starts"]):
        key = "%s#%d" % (today.isoformat(), index)
        if key in persist["skipped_today"] or key in persist["consumed"]:
            continue
        begins = _start_time(entry, local_now, suns)
        if begins is not None and begins <= local_now:
            return {"id": key, "kind": "scheduled",
                    "ends_epoch": local_now.timestamp() + entry["run"]}
    return None


def _start_refusal(spec, request, readings, persist, state, mono_now,
                   local_now, inverter_forced_off, floor_reason):
    """Why a run cannot start now, or None when it can."""
    if _locked_out(persist):
        return "locked out after %d failed starts today" % (
            persist.get("failed_today", 0))
    if persist.get("amp_hours_today", 0.0) >= spec["limits"]["max_amp_hours"]:
        return "the daily limit of %g Ah is used" % (
            spec["limits"]["max_amp_hours"])
    if inverter_forced_off:
        return "the inverter is forced off"
    if floor_reason:
        return floor_reason
    start = spec["start"]
    reason = _battery_fails(_value(readings, spec, "soc"),
                            _value(readings, spec, "volts"),
                            start["soc_above"], start["volts_above"])
    if reason:
        return reason
    if request["kind"] == "manual":
        return None
    seen = state.get("charge_state_seen")
    if not isinstance(seen, (int, float)) or (
            mono_now - seen > start["charge_state_within"]):
        return ("the charge controller has not been in %s within the last "
                "%d h" % (" or ".join(start["charge_state_names"]),
                          start["charge_state_within"] // 3600))
    last_end = persist.get("last_run_end_epoch")
    if isinstance(last_end, (int, float)) and (
            local_now.timestamp() - last_end < spec["limits"]["cooldown"]):
        return "in the cooldown after the last run"
    return None


# -- a run --------------------------------------------------------------------

def _decision(name, on, code, reason, informed):
    return decide.Decision(name, on, 0, code, reason, informed)


def _remember(persist, request_id):
    used = [entry for entry in persist["consumed"] if entry != request_id]
    used.append(request_id)
    persist["consumed"] = used[-REMEMBERED_REQUESTS:]


def _close_run(state, persist, local_now, clock_trusted):
    """Record the end of a run, whatever ended it."""
    run = state.pop("run", None) or {}
    # The end is recorded against a trusted clock only. Until there is one
    # the run stays marked active, and the first trusted pass closes it, so
    # the cooldown still counts from it.
    if run.get("checked"):
        persist["active_run_watered"] = True
    if clock_trusted:
        persist.pop("active_run", None)
        persist["last_run_end_epoch"] = local_now.timestamp()
        if persist.pop("active_run_watered", False):
            persist["last_run_day"] = local_now.date().isoformat()


def _fail_start(spec, persist):
    persist["failed_today"] = int(persist.get("failed_today", 0)) + 1
    if persist["failed_today"] >= spec["limits"]["failed_start_lockout"]:
        persist["lockout_day"] = persist["day"]


def _count_amp_hours(run, persist, amps, stamp):
    """Add the pump's draw since the last shunt reading it was counted on."""
    pump_on = run.get("pump_on_mono")
    if amps is None or stamp is None or pump_on is None:
        return
    last = run.get("last_shunt_at")
    if stamp <= pump_on or (last is not None and stamp <= last):
        return
    since = pump_on if last is None else max(last, pump_on)
    rise = run["baseline_amps"] - amps
    persist["amp_hours_today"] = float(persist.get("amp_hours_today", 0.0)) + (
        max(0.0, rise) * (stamp - since) / 3600.0)
    run["last_shunt_at"] = stamp


def _running_stop(spec, run, readings, persist, local_now, mono_now,
                  request_live, inverter_forced_off, floor_reason):
    """The reason a run must stop now and whether it is a failed start, or
    (None, False). Counts the amp-hours first, so a stop loses none."""
    amps, stamp = _shunt(readings, spec)
    _count_amp_hours(run, persist, amps, stamp)
    pump_on = run["pump_on_mono"]

    if inverter_forced_off:
        return "the inverter is forced off", False
    if not request_live:
        return "the request ended", False
    if local_now.timestamp() >= run["ends_epoch"]:
        return "the run time ended", False
    elapsed = mono_now - run["started_mono"]
    if elapsed >= spec["absolute_max_run"]:
        return "the absolute limit of %d min" % (
            spec["absolute_max_run"] // 60), False
    if elapsed >= spec["continue"]["max_run"]:
        return "the run limit of %d min" % (
            spec["continue"]["max_run"] // 60), False
    reason = _keep_fails(spec, readings, floor_reason)
    if reason:
        return reason, False
    if amps is None:
        # Before the check, a shunt that has gone quiet is a pump nobody has
        # seen draw anything, and it counts as a failed start like one.
        return ("no fresh shunt reading, and irrigation does not run blind",
                not run.get("checked"))
    if persist["amp_hours_today"] >= spec["limits"]["max_amp_hours"]:
        return "the daily limit of %g Ah is used" % (
            spec["limits"]["max_amp_hours"]), False

    if not run.get("checked"):
        low, high = spec["pump"]["expect_amps"]
        dry = spec["dry_run_check"]
        opens = pump_on + dry["settle"]
        closes = opens + dry["deadline"]
        if stamp is not None and stamp >= opens:
            if stamp > closes:
                return ("no shunt reading came in time to check the pump"),\
                    True
            rise = run["baseline_amps"] - amps
            if rise < low:
                return ("the pump drew %.1f A, below %g A: dry or lost its "
                        "prime" % (rise, low)), True
            if rise > high:
                return ("the pump drew %.1f A, above %g A: stalled"
                        % (rise, high)), True
            run["checked"] = True
            run["checked_amps"] = rise
        elif mono_now > closes:
            return ("no shunt reading came in time to check the pump"), True
    return None, False


def _floor(spec, readings, state):
    """The node's battery floor for this load: a reason while it holds."""
    latched, value, source, key = decide._step_floor(
        {"floor": spec["floor"], "stale_after": spec["stale_after"]},
        readings, state.get("floor_latched", False),
        state.get("floor_latched_by"))
    state["floor_latched"] = latched
    state["floor_latched_by"] = key if latched else None
    if not latched:
        return None
    if source is None or value is None:
        return "the battery floor holds until %s reads clear" % key
    return "the battery floor: %s %.2f is below %g" % (
        key, value, source["restore_above"])


def irrigation_decision(name, spec, readings, local_now, mono_now, suns,
                        clock_trusted, switches, state, inverter_state):
    """Decide the pump and the inverter.

    Returns the pump's decision, the inverter's decision, and the next state
    of each. The pump's decision carries the load's name and the inverter's
    carries the name with INVERTER added, so each one drives its own relay
    and is published as its own load.

    A run moves through these phases, one pass at least in each:

    - idle: a request that passes the start conditions is taken, and its
      record goes to the card before either relay moves
    - starting: once the loop has written the record, the inverter turns on;
      after it settles, and with a fresh shunt reading as the baseline, the
      pump turns on
    - running: until the first stop
    - stopping: the pump is off and the inverter stays on until the pump has
      spun down, then the inverter follows
    """
    state = dict(state)
    inverter_state = dict(inverter_state)
    today = local_now.date().isoformat()
    persist = _persist_for_today(state.get("persist"), today, clock_trusted)
    state["persist"] = persist
    phase = state.get("phase", IDLE)
    soc = _value(readings, spec, "soc")

    code = _value(readings, spec, "charge_state")
    if code is not None and int(code) in spec["start"]["charge_state_codes"]:
        state["charge_state_seen"] = mono_now

    floor_reason = _floor(spec, readings, state)

    # The relays dropped when the last process ended, so a run it left
    # behind is over. Starting it again would run the sequence once per
    # restart. A run that a reboot ended is closed the same way, from the
    # record on the card, so the cooldown still counts from it.
    restarted = state.pop("restarted", False)
    if inverter_state.pop("restarted", False) or restarted:
        if phase != IDLE:
            state["phase"] = phase = IDLE
            state["stop_reason"] = "the controller restarted during a run"
            _close_run(state, persist, local_now, clock_trusted)
    if phase == IDLE and persist.get("active_run") and clock_trusted:
        state["stop_reason"] = ("the last run ended by a restart or without a "
                                "trusted clock")
        _close_run(state, persist, local_now, clock_trusted)

    inverter_force = _force(inverter_name(name), switches, local_now,
                            clock_trusted)
    inverter_forced_off = inverter_force == "off"
    pump_forced_off = _force(name, switches, local_now, clock_trusted) == "off"

    clear = (switches.get("loads") or {}).get(name + CLEAR_LOCKOUT)
    if isinstance(clear, dict) and isinstance(clear.get("at"), (int, float)) \
            and not isinstance(clear.get("at"), bool):
        if clear["at"] > persist.get("lockout_cleared_at", 0.0):
            persist["lockout_cleared_at"] = float(clear["at"])
            persist.pop("lockout_day", None)
            persist["failed_today"] = 0

    manual = _manual_request(name, switches, local_now, clock_trusted)
    request = None
    if clock_trusted and not pump_forced_off:
        if manual and manual["id"] not in persist["consumed"]:
            request = manual
        else:
            request = _scheduled_request(spec, persist, local_now, suns)

    pump_on = inverter_on = False
    reason = state.get("stop_reason") or "idle"
    reason_code = decide.NONE

    if phase == IDLE:
        if request is not None:
            refusal = _start_refusal(spec, request, readings, persist, state,
                                     mono_now, local_now, inverter_forced_off,
                                     floor_reason)
            if refusal is None:
                _remember(persist, request["id"])
                if request["kind"] == "scheduled":
                    persist["schedule_day"] = today
                persist["active_run"] = request["id"]
                state["run"] = {"id": request["id"], "kind": request["kind"],
                                "ends_epoch": request["ends_epoch"],
                                "started_mono": mono_now, "armed": False}
                # The loop says whether the record reached the card.
                state["persist_saved"] = None
                state["phase"] = phase = STARTING
                state["phase_since"] = mono_now
                state.pop("stop_reason", None)
                reason = "starting: recording the run"
            else:
                reason = "a %s run cannot start: %s" % (request["kind"],
                                                        refusal)
                reason_code = decide.REFUSED
                if request["kind"] == "scheduled":
                    # Skipped for the day, and the reason stays in view
                    # until something else happens.
                    persist["skipped_today"] = (persist["skipped_today"]
                                                + [request["id"]])
                    state["stop_reason"] = reason
        elif pump_forced_off:
            reason = "forced off by an operator"
            reason_code = decide.OVERRIDE
        elif _locked_out(persist):
            reason = "locked out after %d failed starts today" % (
                persist.get("failed_today", 0))
            reason_code = decide.REFUSED

    run = state.get("run")
    live = phase in (STARTING, RUNNING) and not pump_forced_off and \
        clock_trusted and (run["kind"] == "scheduled" or (
            manual is not None and manual["id"] == run["id"]))
    manual_code = decide.OVERRIDE if run and run["kind"] == "manual" \
        else decide.NONE

    if phase == STARTING and state["phase_since"] != mono_now:
        stop = None
        if inverter_forced_off:
            stop = "the inverter is forced off"
        elif not live:
            stop = "the request ended"
        elif local_now.timestamp() >= run["ends_epoch"]:
            stop = "the run time ended"
        else:
            stop = _keep_fails(spec, readings, floor_reason)
        if stop is None and not run["armed"]:
            if state.get("persist_saved"):
                run["armed"] = True
                state["phase_since"] = mono_now
            else:
                stop = "the run could not be recorded on the card"
        elif stop is None and mono_now - state["phase_since"] >= \
                spec["inverter"]["settle"]:
            amps, _ = _shunt(readings, spec)
            if amps is not None:
                run["baseline_amps"] = amps
                run["pump_on_mono"] = mono_now
                state["phase"] = phase = RUNNING
                pump_on = inverter_on = True
                reason = "running (%s): the pump is starting" % run["kind"]
                reason_code = manual_code
            elif (mono_now - state["phase_since"]
                  > spec["dry_run_check"]["deadline"]):
                stop = "no fresh shunt reading to start the pump against"
        if stop is not None:
            # The pump never ran, so the inverter can go at once.
            _close_run(state, persist, local_now, clock_trusted)
            state["stop_reason"] = reason = "stopped: " + stop
            state["phase"] = phase = IDLE
        elif phase == STARTING and run["armed"]:
            inverter_on = True
            reason = "starting: the inverter is settling"
            reason_code = manual_code
    elif phase == RUNNING:
        stop, failed = _running_stop(spec, run, readings, persist, local_now,
                                     mono_now, live, inverter_forced_off,
                                     floor_reason)
        if stop is None:
            pump_on = inverter_on = True
            reason = "running (%s)" % run["kind"]
            if run.get("checked"):
                reason += ", the pump draws %.1f A" % run["checked_amps"]
            reason_code = manual_code
        else:
            if failed:
                _fail_start(spec, persist)
            _close_run(state, persist, local_now, clock_trusted)
            state["stop_reason"] = reason = "stopped: " + stop
            reason_code = decide.REFUSED if failed else decide.NONE
            state["phase"] = phase = STOPPING
            state["phase_since"] = mono_now
            # The inverter stays on this pass, with the pump off.
            inverter_on = True
    elif phase == STOPPING:
        if mono_now - state["phase_since"] >= spec["pump"]["settle_off"]:
            state["phase"] = phase = IDLE
        else:
            # Whatever else is asked, the inverter stays on until the pump
            # has spun down: an inverter forced off waits for this too.
            inverter_on = True
            reason = state.get("stop_reason") or "stopping"

    # The inverter alone, for AC power. It stops at the battery floor and
    # the continue minimums, and it comes back only above the start
    # minimums, so it does not cycle on a threshold.
    inverter_reason = None
    if inverter_force == "on":
        start = spec["start"]
        volts = _value(readings, spec, "volts")
        if state.get("inverter_held"):
            low = floor_reason or _battery_fails(
                soc, volts, start["soc_above"], start["volts_above"])
            if low is None:
                state.pop("inverter_held", None)
        else:
            low = _keep_fails(spec, readings, floor_reason)
            if low is not None:
                state["inverter_held"] = True
        if state.get("inverter_held"):
            if not inverter_on:
                inverter_reason = ("forced on, held off by the battery: %s"
                                   % low)
        else:
            inverter_on = True
            inverter_reason = "forced on by an operator"
    elif inverter_force != "on":
        state.pop("inverter_held", None)

    if inverter_reason is None:
        if inverter_on:
            inverter_reason = "on for the pump"
        elif inverter_forced_off:
            inverter_reason = "forced off by an operator"
        else:
            inverter_reason = "off"

    if pump_on and not inverter_on:
        raise AssertionError("the pump must never run without the inverter")

    state["commanded"] = pump_on
    inverter_state["commanded"] = inverter_on
    state["published_extra"] = {
        "phase": PHASE_CODES[phase],
        "amp_hours_today": float(persist.get("amp_hours_today", 0.0)),
        "failed_starts_today": float(persist.get("failed_today", 0)),
        "locked_out": 1.0 if _locked_out(persist) else 0.0,
        "floor_latched": 1.0 if state.get("floor_latched") else 0.0,
    }
    informed = soc if soc is not None else (0.0 if inverter_on else None)
    pump = _decision(name, pump_on, reason_code, reason, informed)
    inverter = _decision(
        inverter_name(name), inverter_on,
        decide.OVERRIDE if inverter_force else decide.NONE, inverter_reason,
        informed)
    return pump, inverter, state, inverter_state
