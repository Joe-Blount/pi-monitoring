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

    #: Whether this driver needs a Bluetooth stack. Declared here rather
    #: than guessed from the driver's name by the install script, which once
    #: matched only names beginning "ble_" and therefore skipped bleak for a
    #: Bluetooth driver named after its protocol.
    needs_bluetooth = False

    #: Modes this driver can actually work in. A rain gauge in poll mode
    #: validates, lists, checks and publishes a total that never changes,
    #: because nothing is watching the pin between polls.
    modes = ("poll", "resident", "controller")

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
