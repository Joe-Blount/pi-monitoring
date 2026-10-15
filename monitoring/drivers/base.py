"""What every driver is, and what it is not.

A driver reads one device and returns values. It never writes to InfluxDB,
never reads the environment, never decides an interval and never sleeps.
Everything else is the runner's job.

Keeping it that narrow is what makes the drivers testable on a laptop with no
hardware, no network and no credentials.
"""


class DriverError(Exception):
    """A device that could not be read.

    Raised rather than returned, and never swallowed: a device that fails
    must be visible in the log, because silence has twice been mistaken for
    health here.
    """


class Driver:
    """Base class. Subclasses implement read(), stream(), or both."""

    #: Set by subclasses. Used in error messages and by --list.
    description = ""

    #: Whether one read can take tens of seconds.
    #:
    #: Poll devices are read by one process on one schedule, and telegraf
    #: kills that process at its timeout. So a slow device sharing a schedule
    #: with fast ones can take the whole node's telemetry down: the kill
    #: discards every reading taken before it, including the points that would
    #: have reported the failure.
    #:
    #: Slow devices therefore get their own collector, with its own interval
    #: and its own much longer timeout.
    slow_read = False

    #: Whether this driver needs a Bluetooth stack. Declared here rather
    #: than guessed from the driver's name by the install script, which once
    #: matched only names beginning "ble_" and therefore skipped bleak for a
    #: Bluetooth driver named after its protocol.
    needs_bluetooth = False

    #: Modes this driver can actually work in. A rain gauge in poll mode
    #: validates, lists, checks and publishes a total that never changes,
    #: because nothing is watching the pin between polls.
    modes = ("poll", "resident")

    def __init__(self, name, params, tags):
        self.name = name
        self.params = dict(params or {})
        self.tags = dict(tags or {})

    def read(self):
        """Return a dict of field name to value.

        Numbers and strings only. None means "no reading for this field",
        and the field is dropped rather than written.
        """
        raise NotImplementedError("%s does not support polling" % type(self).__name__)

    def stream(self):
        """Yield dicts of field name to value as readings arrive.

        Implemented by drivers whose device talks when it feels like it: a
        rain gauge counting pulses, a controller sending unprompted frames.
        """
        raise NotImplementedError("%s does not support streaming" % type(self).__name__)

    def check(self):
        """Confirm the device is present, without taking a reading.

        Returns a short string describing what was found. Raises DriverError
        if the device is not there. Used by `collect --check` during bring-up,
        so a wiring mistake is found while standing next to the hardware.
        """
        raise DriverError("%s cannot be checked" % type(self).__name__)

    def raw(self):
        """Return the device's unparsed output, for debugging a new device."""
        raise DriverError("%s has no raw mode" % type(self).__name__)

    def close(self):
        """Release anything held. Safe to call more than once."""


def required(params, key, name):
    """Fetch a parameter that has no sensible default."""
    if key not in params:
        raise DriverError("device %r needs the parameter %r" % (name, key))
    return params[key]


#: Extra time allowed past a read's own deadline before it is abandoned
#: outright. The deadline normally fires first. This covers the case where
#: cancelling the read is itself what hangs, which is the usual one: tearing
#: down a connection waits on the system bus, and the bus is what is already
#: wedged when a deadline fires.
JOIN_GRACE_SECONDS = 5.0

#: Reads that were abandoned and are still running, keyed by whatever
#: identifies the device. A one-shot collection abandons at most one and then
#: exits. A controller loop would call back every few seconds, and each
#: abandoned read keeps a thread, an event loop and a connection alive, with
#: the stale connection blocking the next one to the same device. So a second
#: read is refused while the first has not returned, which bounds the damage
#: to one and makes the fault visible instead of silently stacking.
_ABANDONED = {}


def run_abandonable(label, budget, work, grace=None):
    """Run `work` on a thread this call is willing to walk away from.

    A deadline inside the work is not enough on its own. Cancelling a
    coroutine waits for its cleanup, and closing a device connection waits on
    the system bus, which is exactly the thing already wedged when the
    deadline fires. So the inner deadline can overrun itself, and the caller
    must not depend on it.

    The thread is a daemon, so a device that never returns cannot keep the
    collector alive. This matters because the install script stops telegraf
    and then checks every device: a read that hangs there leaves a whole node
    silent, at a site an hour away, with nothing in any log saying why.
    """
    import threading

    # Read at call time, not bound as a default. A default argument is
    # evaluated once when the function is defined, so the constant above
    # could never be adjusted, by a test or by anything else.
    if grace is None:
        grace = JOIN_GRACE_SECONDS

    stuck = _ABANDONED.get(label)
    if stuck is not None:
        if stuck.is_alive():
            raise DriverError(
                "an earlier read of %s has still not returned, so this one "
                "was not started. The device or the system bus is wedged"
                % label)
        del _ABANDONED[label]

    box = {}

    def run():
        try:
            box["result"] = work()
        except BaseException as exc:          # noqa: BLE001
            box["error"] = exc

    worker = threading.Thread(target=run, daemon=True, name="read-%s" % label)
    worker.start()
    worker.join(budget + grace)

    if worker.is_alive():
        _ABANDONED[label] = worker
        raise DriverError(
            "%s did not return within %g seconds even after its deadline, so "
            "it was abandoned rather than allowed to hold this node"
            % (label, budget + grace))
    if "error" in box:
        raise box["error"]
    return box["result"]
