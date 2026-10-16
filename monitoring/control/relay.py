"""Driving a relay, and the one constant that must never be wrong.

This file is short and it is the most dangerous part of the controller.
Everything else decides whether a load should be connected; this turns that
into a voltage on a pin, and the translation can be inverted by a single
word in a node file.

Polarity
--------
A relay board carries a common terminal, a normally open one and a normally
closed one. Which one the load is wired to decides what happens when nothing
is driving the pin, and that is the state the machine sits in while the Pi is
off, booting, crashed, or has had its controller killed.

So polarity is not a preference. It is the answer to "what should happen when
this software is not running", and the two loads here want opposite answers:

``energize_to_shed``
    De-energized connects the load. The cameras and the recorder are wired
    this way, so a dead Pi leaves the site watched and a reboot does not
    power-cycle a recorder.

``energize_to_connect``
    De-energized disconnects the load. Irrigation is wired this way, because
    a pump running unattended because a Pi rebooted is a different kind of
    problem from a dark camera.

Getting that backwards does not fail loudly. It produces a system that runs
the cameras all night and sheds them all day, or one that waters the field
every time the power flickers. There is no reading that reveals it and no
alarm that fires. The tests therefore assert the **pin level** for a given
command, not the command, because a test that checks "the controller decided
to connect" passes just as happily while the pin drives the opposite way.

Trigger level
-------------
Separate from polarity and just as easy to get wrong, but it lives in
hardware. These boards select whether a relay energizes on a high input or a
low one. On a low-trigger board the module's own pull-up beats the Pi's weak
internal pull-down and every relay energizes during boot, before any software
runs. That inverts the fail-safe for every load at once, and the only check
on it is a person watching the indicator LEDs through a complete boot with
the loads disconnected. It is in the commissioning steps for that reason.

One path, not two
-----------------
The injected factory and the shipping one are the same call with the same
arguments, and the dry run goes through the same ``apply``. An earlier version
had the test double constructed differently from the real device and the dry
run computing its own level, which left two of the three places polarity can
invert unreachable from a test: the library's own ``active_high``, which
inverts the claim as well as every level after it, and the branch that turns a
level into a call.

Releasing the line
------------------
The whole fail-safe story rests on a dying process releasing its pins. The
character device backend does; the sysfs one does not, and leaves a pin
driven and busy so the next start cannot even claim it. The installer pins
the backend for exactly this reason, and this module asks for it again rather
than trusting the environment it finds.
"""

import os

#: De-energized connects the load. For anything whose absence is safer than
#: its absence of control.
ENERGIZE_TO_SHED = "energize_to_shed"

#: De-energized disconnects the load.
ENERGIZE_TO_CONNECT = "energize_to_connect"

POLARITIES = (ENERGIZE_TO_SHED, ENERGIZE_TO_CONNECT)

#: The gpiozero backend that releases a pin when the process holding it dies.
#: Set before gpiozero is imported, because it reads this at import time, and
#: set rather than defaulted: an environment that already names another one
#: would otherwise win silently, and the one it would most likely name is the
#: fallback that leaves a pin driven and busy when the process dies.
PIN_FACTORY = "lgpio"


class RelayError(RuntimeError):
    """A pin that cannot be driven."""


def open_output(pin, initial_value=False):
    """Claim one output pin through gpiozero.

    `active_high` is stated rather than left to the library's default. It is
    the third place polarity can invert, and the most dangerous, because it
    inverts the state the pin is claimed in as well as every level after:
    with it wrong, claiming the camera relay energizes it, cutting the
    recorder, and then every later decision drives the opposite way.
    """
    os.environ["GPIOZERO_PIN_FACTORY"] = PIN_FACTORY
    try:
        from gpiozero import DigitalOutputDevice
    except ImportError as exc:
        raise RelayError("load control needs gpiozero: %s" % exc)
    try:
        return DigitalOutputDevice(pin, initial_value=initial_value,
                                   active_high=True)
    except Exception as exc:
        raise RelayError("cannot drive GPIO%s: %s" % (pin, exc))


class Recording:
    """An output pin that remembers instead of existing.

    Constructed exactly as the real one is, so a test exercises the call the
    shipping code makes rather than a convenience signature.
    """

    def __init__(self, pin, initial_value=False, active_high=True):
        self.pin = pin
        self.active_high = active_high
        self.claimed = bool(initial_value)
        self.levels = []
        self.closed = False

    def on(self):
        self.levels.append(True)

    def off(self):
        self.levels.append(False)

    def close(self):
        self.closed = True


def level_for(polarity, connected):
    """The pin level that puts a load in the wanted state.

    One function, deliberately, and every decision about polarity in the
    system goes through it. The inversion this exists to prevent is a
    one-token edit, so it is worth having exactly one place to make it and
    exactly one place to test it.
    """
    if polarity == ENERGIZE_TO_SHED:
        return not connected
    if polarity == ENERGIZE_TO_CONNECT:
        return bool(connected)
    raise RelayError(
        "polarity %r is neither %s nor %s, so what a dead controller leaves "
        "behind is undefined" % (polarity, ENERGIZE_TO_SHED,
                                 ENERGIZE_TO_CONNECT))


class Relay:
    """One output pin, driven through gpiozero.

    Opened lazily. A controller that cannot claim a pin should fail when it
    tries to drive it, naming the pin, rather than at import where the error
    reads like a missing package.
    """

    def __init__(self, pin, polarity, factory=None):
        if polarity not in POLARITIES:
            raise RelayError(
                "load on GPIO%s declares polarity %r; it must be one of %s"
                % (pin, polarity, ", ".join(POLARITIES)))
        self.pin = pin
        self.polarity = polarity
        self._factory = factory or open_output
        self._device = None

    def _open(self):
        if self._device is None:
            # initial_value False so claiming the pin does not drive it high
            # on the way past. On a shed-to-energize load that would cut the
            # recorder for the width of the call.
            self._device = self._factory(self.pin, initial_value=False)
        return self._device

    def apply(self, connected):
        """Put the load in `connected`, and return the level driven."""
        level = level_for(self.polarity, connected)
        device = self._open()
        if level:
            device.on()
        else:
            device.off()
        return level

    def close(self):
        if self._device is None:
            return
        try:
            self._device.close()
        finally:
            self._device = None


class Pretend(Relay):
    """A relay that records instead of driving, for a dry run.

    It swaps the device, not the logic. An earlier version overrode `apply`,
    which left the branch turning a level into a call unreachable from a dry
    run: a dry run on site would then have shown the right answer while the
    real relay drove the wrong way.
    """

    def __init__(self, pin, polarity):
        Relay.__init__(self, pin, polarity, factory=Recording)

    @property
    def levels(self):
        return self._device.levels if self._device is not None else []
