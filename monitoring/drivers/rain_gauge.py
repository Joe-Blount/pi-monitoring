"""Tipping bucket rain gauge.

A small see-saw bucket tips each time it fills, closing a reed switch against
ground. Each tip is a fixed amount of rain, so rainfall is a count multiplied
by a constant.

Must be resident. A tip happens when it happens, and a process that only looks
every thirty seconds misses them.

The count is persisted. Held only in memory it resets on every power event,
and the graph then shows rainfall that stopped rather than a Pi that
restarted, which is a quietly wrong answer rather than a visibly missing one.

The counting and the persistence are deliberately separate from the GPIO
wiring, so the part with the arithmetic in it can be tested without a Pi, a
switch, or a GPIO library.
"""

import json
import os
import tempfile
import time

from ..duration import seconds as _seconds
from .base import Driver, DriverError

DEFAULT_INCHES_PER_TIP = 0.011


class RainCounter:
    """Tips and total rainfall, persisted across restarts.

    Kept free of any GPIO dependency so it can be exercised directly.
    """

    def __init__(self, state_file, inches_per_tip=DEFAULT_INCHES_PER_TIP,
                 clock=time.time):
        self.state_file = state_file
        self.inches_per_tip = float(inches_per_tip)
        self.clock = clock
        self.tips = 0
        self.last_tip = None
        self.load()

    # -- persistence ------------------------------------------------------

    def load(self):
        """Restore the count, treating any problem as a fresh start.

        A corrupt or missing state file must not stop the gauge working. A
        lost total is a gap in one series; refusing to start loses the
        readings from then on as well.
        """
        if not self.state_file:
            return
        try:
            with open(self.state_file) as handle:
                saved = json.load(handle)
            self.tips = int(saved.get("tips", 0))
            self.last_tip = saved.get("last_tip")
        except (OSError, ValueError, TypeError):
            self.tips = 0
            self.last_tip = None

    def save(self):
        """Write the count so a restart does not lose it.

        Written to a temporary file and renamed, because a power cut partway
        through writing the real file would leave something unparsable, and
        the whole point of this file is to survive power cuts. A rename is
        atomic, so the file is either the old count or the new one.
        """
        if not self.state_file:
            return
        directory = os.path.dirname(self.state_file) or "."
        temporary = None
        try:
            os.makedirs(directory, exist_ok=True)
            handle = tempfile.NamedTemporaryFile(
                mode="w", dir=directory, delete=False, prefix=".rain-")
            temporary = handle.name
            try:
                json.dump({"tips": self.tips, "last_tip": self.last_tip}, handle)
                handle.flush()
                os.fsync(handle.fileno())
            finally:
                handle.close()
            os.replace(temporary, self.state_file)
            temporary = None
        except Exception:
            # Every failure, not only OSError: a path containing a null byte
            # raises ValueError, and a surprise from the filesystem must not
            # be the thing that stops rain being counted. Persistence is
            # best effort; counting is not.
            if temporary:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass

    # -- counting ---------------------------------------------------------

    def tip(self):
        """Record one bucket tip."""
        self.tips += 1
        self.last_tip = round(self.clock(), 3)
        self.save()
        return self.tips

    @property
    def inches(self):
        return round(self.tips * self.inches_per_tip, 4)

    def fields(self):
        """What this gauge publishes.

        `rain_inches` counts up and never resets, which is what a rainfall
        total should do. Ask a query for the increase over a window rather
        than expecting the number itself to go back to zero.
        """
        out = {"rain_inches": self.inches, "rain_tips": float(self.tips)}
        if self.last_tip is not None:
            out["seconds_since_tip"] = round(self.clock() - self.last_tip, 1)
        return out


class RainGaugeDriver(Driver):
    """Count tips on a GPIO pin and publish the running total."""

    description = "tipping bucket rain gauge"

    def __init__(self, name, params, tags):
        Driver.__init__(self, name, params, tags)
        self.pin = self.params.get("pin")
        if self.pin is None:
            raise DriverError("device %r needs the parameter 'pin'" % name)
        self.bounce_seconds = float(self.params.get("bounce_ms", 1)) / 1000.0
        self.counter = RainCounter(
            self.params.get("state_file"),
            self.params.get("inches_per_tip", DEFAULT_INCHES_PER_TIP))
        self.heartbeat = _seconds(self.params.get("emit_every", "60s"))
        self._button = None

    def _open(self):
        """Attach to the pin, lazily, so importing needs no GPIO library."""
        if self._button is not None:
            return self._button
        try:
            from gpiozero import Button
        except ImportError:
            raise DriverError(
                "the rain_gauge driver needs gpiozero "
                "(apt install python3-gpiozero)")
        try:
            # The switch closes to ground, so the pin is pulled up and a tip
            # reads as a falling edge. bounce_time absorbs the contact chatter
            # a mechanical reed switch produces, which would otherwise count
            # one tip several times.
            self._button = Button(self.pin, pull_up=True,
                                  bounce_time=self.bounce_seconds)
        except Exception as exc:
            raise DriverError("cannot attach to GPIO%s: %s" % (self.pin, exc))
        self._button.when_pressed = lambda *_: self.counter.tip()
        return self._button

    def stream(self):
        """Publish on every tip, and regularly even when it is not raining.

        Without the regular publication the series would simply stop in dry
        weather, which is indistinguishable from the gauge having failed.
        """
        self._open()
        last = 0.0
        last_tips = -1
        while True:
            now = time.time()
            if self.counter.tips != last_tips or now - last >= self.heartbeat:
                last, last_tips = now, self.counter.tips
                yield self.counter.fields()
            time.sleep(0.2)

    def read(self):
        """Return the current total without waiting for a tip."""
        return self.counter.fields()

    def check(self):
        self._open()
        return "GPIO%s attached, %d tip(s) carried over (%s in)" % (
            self.pin, self.counter.tips, self.counter.inches)

    def close(self):
        if self._button is not None:
            try:
                self._button.close()
            finally:
                self._button = None
