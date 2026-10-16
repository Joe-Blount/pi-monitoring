"""The service: everything ambient on one side, the decision on the other.

This is the only module that reads a clock, touches a file or drives a pin.
It gathers, calls `decide`, applies what came back, and publishes what it
did. The judgment lives in `decide`, which is pure, so the awkward parts are
testable without waiting for real time to pass or owning a relay.

What it will not do
-------------------
**It does not drive a relay before a reading justifies it.** Starting up, the
loop touches nothing. Every load's de-energized state is its fail-safe, which
is the state the machine already sits in while the controller is not running,
so acting ahead of information buys nothing and costs a recorder cycle on
every restart. The first pass with a reading in hand acts, once.

**It does not advance the clock's high-water mark from an untrusted clock.**
One fast wrong reading recorded there would be a bound no real clock could
ever clear.

**It does not let a failure to publish stop it controlling.** Publishing is
how somebody finds out what happened; controlling is the job.

**It does not start without a boot identity.** Every stamp the loop keeps is
on the monotonic clock, which restarts at zero each boot, so the identity is
what makes a stored stamp comparable with a current one. Without it the state
file is discarded on every read, the tier ratchet silently stops existing,
and a tier would release the moment the battery rose a point. That is a
degradation nobody would notice from the data, so it refuses instead.
"""

import datetime

from . import clock, decide, files, relay, spec, sun


class LoopError(RuntimeError):
    """The loop cannot run, and will not pretend to."""


class Loop:
    """One node's control loop.

    Everything it needs from the world is injected, so a test can run a
    thousand passes against a fake clock and a recording relay. The defaults
    are what the service uses.
    """

    def __init__(self, node, location, zone, relays=None, now=None,
                 monotonic=None, trust=None, readings=None, switches=None,
                 state_path=None, high_water_path=None, publish=None,
                 published_path=None, boot=None):
        self.node = node
        self.specs = spec.load_control(node)

        self._boot = files.boot_id() if boot is None else boot
        if not self._boot:
            raise LoopError(
                "cannot read a boot identity, so a stored monotonic stamp "
                "cannot be told from one belonging to a previous boot. The "
                "tier ratchet depends on it, and without it every tier would "
                "release as soon as the battery rose a point")
        self.location = location
        self.zone = zone

        self._relays = relays if relays is not None else {
            name: relay.Relay(load["control_pin"], load["polarity"])
            for name, load in self.specs.items()}

        self._now = now or (lambda: datetime.datetime.now(self.zone))
        self._monotonic = monotonic or files.monotonic
        self._trust = trust or clock.assess
        self._readings = readings or files.read_readings
        self._switches = switches or files.read_switches
        self._state_path = state_path
        self._published_path = published_path
        self._high_water_path = high_water_path
        self._publish = publish if publish is not None else files.write_json

        self.state = files.read_state(self._state_path,
                                      this_boot=self._boot)
        self.started = self._monotonic()
        self.passes = 0

    # -- the sun ------------------------------------------------------------

    def _suns(self, local_now):
        """Today's events and its neighbours'.

        A window is an offset from an event, so one reaching far enough past
        sunset belongs partly to the next calendar day, and the hour after
        midnight falls inside a window anchored on yesterday.
        """
        today = local_now.date()
        out = []
        for offset in (-1, 0, 1):
            day = today + datetime.timedelta(days=offset)
            rise, _, set_ = sun.events(day, self.location["latitude"],
                                       self.location["longitude"], self.zone)
            out.append({"sunrise": rise, "sunset": set_})
        return out

    # -- one pass -----------------------------------------------------------

    def once(self, apply=True, persist=True):
        """Gather, decide, apply, publish. Returns the decisions.

        `persist` false leaves no trace: no state file, no published
        document, no advance of the clock's high-water mark. A dry run has
        to be that, or looking at a machine changes it. The published
        document is what the controller reports to the database, so a dry
        run writing it would replace a live load's status with a decision
        that was never carried out, and somebody reading a dashboard would
        see a shed that never happened.
        """
        local_now = self._now()
        mono_now = self._monotonic()

        held = files.read_high_water(self._high_water_path)
        verdict = self._trust(high_water=held)
        if verdict.trusted and persist:
            # Only from a clock that was trusted when it was read.
            files.note_high_water(local_now.timestamp(),
                                  self._high_water_path)

        try:
            # Skipped on an untrusted clock, and the decision would come out
            # the same either way, since a schedule is not applied without a
            # clock to place it against. Two smaller reasons to keep it: there
            # is no point doing astronomy nobody will read, and a failure
            # inside it would replace the real reason the clock is distrusted
            # with a complaint about the sun.
            suns = self._suns(local_now) if verdict.trusted else []
        except sun.NoSunEvent:
            # Impossible at these latitudes, and a schedule that cannot be
            # placed is a schedule that must not be applied.
            suns, verdict = [], clock.Trust(
                False, "the sun does not rise or set here on this date")

        decisions, self.state = decide.decide(
            local_now, mono_now, verdict.trusted,
            self._readings(), self._switches(), self.specs, self.state, suns)

        if apply:
            self._apply(decisions)

        self.passes += 1
        if persist:
            files.write_state(self.state, self._state_path, boot=self._boot)
            self._publish_state(decisions, local_now, mono_now, verdict)
        return decisions

    def _apply(self, decisions):
        """Drive each pin, and record the level that actually went out.

        Nothing is driven for a load until some reading has informed a
        decision about it. Until then the pin is left alone, which leaves the
        load in its de-energized fail-safe, which is the state the machine
        was already in while the controller was not running. Acting ahead of
        information buys nothing and costs a recorder power cycle on every
        start.

        After the first time a load has been driven, every pass drives it,
        including a pass that is only holding. That is deliberate: within one
        boot the loop's state survives a crash while the pin does not, since
        the line is released when the process dies, so a restarted loop must
        put the load back where it had it rather than wait for fresh news.

        A pin that cannot be driven is fatal rather than logged. The loop
        exists to move relays; one it cannot move is not a degraded service,
        it is a load in an unknown state, and the supervisor restarting the
        process is a better answer than carrying on and publishing numbers
        that describe an intention.
        """
        for name, decision in decisions.items():
            pin = self._relays.get(name)
            if pin is None:
                continue
            held = self.state.setdefault(name, {})
            informed = decision.decided_on is not None
            if not held.get("applied") and not informed:
                continue
            held["level"] = pin.apply(decision.connected)
            held["applied"] = True

    # -- what it tells the world -------------------------------------------

    def _publish_state(self, decisions, local_now, mono_now, verdict):
        fields = {
            "clock_synced": 1.0 if verdict.trusted else 0.0,
            "clock_reason": verdict.reason,
            "passes": float(self.passes),
            "loop_monotonic": float(mono_now),
        }
        for name, decision in decisions.items():
            fields.update({
                "%s_powered" % name: 1.0 if decision.connected else 0.0,
                "%s_level" % name: 1.0 if (
                    self.state.get(name, {}).get("level")) else 0.0,
                "%s_shed_level" % name: float(decision.tier),
                "%s_reason_code" % name: float(decision.reason_code),
                "%s_reason" % name: decision.reason,
                "%s_schedule_suspended" % name: 1.0 if (
                    decision.reason_code == decide.SCHEDULE_SUSPENDED) else 0.0,
            })
            if decision.decided_on is not None:
                fields["%s_decided_on" % name] = float(decision.decided_on)

        try:
            self._publish(self._published_path or files.PUBLISHED,
                          {"fields": fields, "monotonic": float(mono_now),
                           "boot_id": self._boot})
        except Exception:
            # Publishing is how somebody finds out. Controlling is the job.
            pass

    def close(self):
        for pin in self._relays.values():
            pin.close()
