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
Separate from polarity and just as easy to get wrong. A relay board
energizes its coil on a high input or on a low one, and the node file says
which, because the two drive every load in opposite directions. ``high``
and ``low`` change only which voltage on the pin energizes the coil. What
this module reports, and what the loop publishes, is whether the coil is
energized, whichever board carries it.

A low trigger board energizes a coil when its input is pulled to ground, so
a pin claimed low would cut the cameras and start the pump for the width of
the claim. Every pin is claimed at the voltage that leaves its coil
de-energized.

The trigger also decides what an undriven pin does, before any software
runs and after a controller dies. The Pi holds such a pin low through a weak
internal pull-down. A high trigger board reads that as off. A low trigger
board with its input side powered from the Pi's 3.3 V sees too little
current through that pull-down to energize a coil, but that is a property of
the board, not of this code. The only check on either is a person watching
the indicator LEDs through a complete boot, and through a killed
controller, with the loads disconnected. It is in the commissioning steps
for that reason.

One path, not two
-----------------
The injected factory and the shipping one are the same call with the same
arguments, and the dry run goes through the same ``apply``. Both have to hold:
a test double constructed differently from the real device, or a dry run that
computes its own level instead of calling ``apply``, leaves two of the three
places polarity can invert unreachable from a test -- the library's own
``active_high``, which inverts the claim as well as every level after it, and
the branch that turns a level into a call.

Releasing the line
------------------
The whole fail-safe story rests on a dying process releasing its pins. The
character device backend does; the sysfs one does not, and leaves a pin
driven and busy so the next start cannot even claim it. The installer pins
the backend for exactly this reason, and this module asks for it again rather
than trusting the environment it finds.

The backend is not the whole of it. A Raspberry Pi kernel can be built to
keep an output driven after the line is freed, through a parameter of its
pin controller, and some ship with it turned on. There the
character device frees the claim and the pin goes on driving anyway. No pin
is claimed while that parameter is on, and the controller refuses to start
at all: one that never runs leaves every load at its wired fail-safe, which
a dead one on such a kernel would not.
"""

import os

#: De-energized connects the load. For anything whose absence is safer than
#: its absence of control.
ENERGIZE_TO_SHED = "energize_to_shed"

#: De-energized disconnects the load.
ENERGIZE_TO_CONNECT = "energize_to_connect"

POLARITIES = (ENERGIZE_TO_SHED, ENERGIZE_TO_CONNECT)

#: The board energizes a coil on a high input.
HIGH = "high"

#: The board energizes a coil on a low input.
LOW = "low"

TRIGGERS = (HIGH, LOW)

#: The gpiozero backend that releases a pin when the process holding it dies.
#: Set before gpiozero is imported, because it reads this at import time, and
#: set rather than defaulted: an environment that already names another one
#: would otherwise win silently, and the one it would most likely name is the
#: fallback that leaves a pin driven and busy when the process dies.
PIN_FACTORY = "lgpio"


#: The kernel's answer to whether a freed output stays driven, Y or N. Absent
#: on kernels without the setting, which free an output as an input.
PERSIST_PARAMETER = os.environ.get(
    "MONITORING_PERSIST_PARAMETER",
    "/sys/module/pinctrl_bcm2835/parameters/persist_gpio_outputs")

#: The boot parameter that turns it off.
PERSIST_FIX = "pinctrl_bcm2835.persist_gpio_outputs=n"


class RelayError(RuntimeError):
    """A pin that cannot be driven."""


def outputs_persist():
    """True when the kernel keeps an output driven after its owner dies."""
    try:
        with open(PERSIST_PARAMETER) as handle:
            return handle.read().strip().upper().startswith("Y")
    except OSError:
        return False


def refuse_if_outputs_persist(what):
    """Raise rather than drive `what` on a kernel that keeps outputs driven."""
    if outputs_persist():
        raise RelayError(
            "refusing to drive %s: this kernel keeps an output driven after "
            "the process holding it dies, so a crashed controller would leave "
            "its relays where they were. Add %s to the end of the line in "
            "/boot/firmware/cmdline.txt and reboot" % (what, PERSIST_FIX))


def claim_output_at(pin, state):
    """Claim an lgpio pin as an output already at `state`, in one call.

    The library's own claim takes two: the line is claimed as an output at
    0, then written to its starting level. On a low trigger board that 0 is
    a coil energized for as long as the gap between the two lasts, which a
    busy machine can stretch past a relay's pull-in time. lgpio takes the
    starting level in the claim itself, so there is no gap to stretch.
    """
    import lgpio
    if getattr(pin, "_callback", None) is not None:
        pin._callback.cancel()
        pin._callback = None
    lgpio.gpio_claim_output(pin.factory._handle, pin._number,
                            1 if state else 0)


def own_pin(found, pin_class, name):
    """Return `found` only if it claims at its starting level.

    The library keeps one cache of pins for every factory of its kind in a
    process, so a pin built earlier through the stock backend would be
    handed back here and claim at 0. Refused rather than used.
    """
    if not isinstance(found, pin_class):
        raise RelayError(
            "GPIO%s is already held in this process through a backend that "
            "claims an output at 0 before its starting level" % (name,))
    return found


_LGPIO_CLASSES = None


def lgpio_classes():
    """The lgpio factory and pin this module drives through.

    Subclassed only to claim each output at its starting level. Built once,
    on demand: importing the backend imports lgpio, which exists only on the
    machine itself, and a second pair of classes would not recognize the
    pins the first built.
    """
    global _LGPIO_CLASSES
    if _LGPIO_CLASSES is None:
        from gpiozero.pins.lgpio import LGPIOFactory, LGPIOPin

        class Pin(LGPIOPin):
            def output_with_state(self, state):
                claim_output_at(self, state)

        class Factory(LGPIOFactory):
            def __init__(self, chip=None):
                LGPIOFactory.__init__(self, chip)
                self.pin_class = Pin

            def pin(self, name):
                return own_pin(LGPIOFactory.pin(self, name), Pin, name)

        _LGPIO_CLASSES = (Factory, Pin)
    return _LGPIO_CLASSES


_FACTORY = None


def controller_factory():
    """The one pin factory every relay in this process is claimed through.

    Passed to each device rather than installed as the library's default,
    which anything else in the process could have set first.
    """
    global _FACTORY
    if _FACTORY is None:
        _FACTORY = lgpio_classes()[0]()
    return _FACTORY


def open_output(pin, initial_value=False, active_high=True):
    """Claim one output pin through gpiozero.

    `active_high` is passed on rather than left to the library's default. It
    is the third place polarity can invert, and the most dangerous, because
    it inverts the state the pin is claimed in as well as every level after:
    with it wrong, claiming a camera relay energizes it, cutting that
    branch, and then every later decision drives the opposite way. It comes
    from the board's trigger, and nowhere else.
    """
    os.environ["GPIOZERO_PIN_FACTORY"] = PIN_FACTORY
    refuse_if_outputs_persist("GPIO%s" % pin)
    try:
        from gpiozero import DigitalOutputDevice
    except ImportError as exc:
        raise RelayError("load control needs gpiozero: %s" % exc)
    try:
        return DigitalOutputDevice(pin, initial_value=initial_value,
                                   active_high=active_high,
                                   pin_factory=controller_factory())
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

    def __init__(self, pin, polarity, trigger, factory=None):
        if polarity not in POLARITIES:
            raise RelayError(
                "load on GPIO%s declares polarity %r; it must be one of %s"
                % (pin, polarity, ", ".join(POLARITIES)))
        if isinstance(trigger, bool) or trigger not in TRIGGERS:
            raise RelayError(
                "load on GPIO%s is on a board with trigger %r; it must be one "
                "of %s" % (pin, trigger, ", ".join(TRIGGERS)))
        self.pin = pin
        self.polarity = polarity
        self.trigger = trigger
        self._factory = factory or open_output
        self._device = None

    def _open(self):
        if self._device is None:
            # initial_value False so claiming the pin does not energize the
            # coil on the way past. On a shed-to-energize load that would cut
            # the recorder for the width of the call. The library turns that
            # into a high pin on a low trigger board.
            self._device = self._factory(self.pin, initial_value=False,
                                         active_high=self.trigger == HIGH)
        return self._device

    def apply(self, connected):
        """Put the load in `connected`, and return whether the coil is now
        energized."""
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

    It swaps the device, not the logic. Overriding `apply` instead would
    leave the branch turning a level into a call unreachable from a dry run,
    so a dry run on site could show the right answer while the real relay
    drove the wrong way.
    """

    def __init__(self, pin, polarity, trigger):
        Relay.__init__(self, pin, polarity, trigger, factory=Recording)

    @property
    def levels(self):
        return self._device.levels if self._device is not None else []
