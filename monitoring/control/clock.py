"""Whether the clock can be trusted to place a solar window.

Two of the three camera tiers are schedules, and one of them is defined
against sunrise and sunset. Both need the date and the time to be right. A
machine that is off-grid, has no uplink, and may have no real-time clock can
come up with neither, and the failure is quiet.

Why the obvious checks do not work
----------------------------------
**Is the clock later than some fixed date?** No. This distribution restores
the time saved at the last shutdown, so a machine with no clock hardware comes
up hours or days stale while looking entirely plausible. Any fixed-date check
passes. The restore is not specific to one package either: where
``fake-hwclock`` has been dropped, the time synchronisation daemon does the
same thing from its own saved stamp, so naming the package would be chasing
the wrong thing.

**Does /dev/rtc0 exist?** No, and this is the one that looks right. The device
node appears whenever an RTC driver registers, which happens before anything
reads the chip. A clock module with a dead coin cell registers, fails its
first read, and leaves the restored stale time in place. Some boards also
register an RTC that ships with no battery at all, so the node exists on a
machine whose clock resets on every power cut.

What works
----------
Either the time synchronisation daemon says it has synchronised, or the RTC
reports a time that is not earlier than the latest time this controller has
already seen. The second is the fixed-date check made sound: the bound is the
machine's own last known time rather than a constant, so it needs no date
written down, it tightens by itself, and a reset clock reads below it.

A dead cell fails the read and is untrusted. A reset board reads earlier than
the high-water mark and is untrusted. A working RTC reads later and is
trusted, with drift of minutes a year against windows measured in hours.

The high-water mark is only advanced from a clock that was trusted at the
time, or a fast wrong clock would poison it permanently.
"""

import os

#: Written by systemd's time synchronisation daemon once it has actually
#: synchronised, and in /run so it cannot survive a reboot.
SYNCHRONISED_MARKER = "/run/systemd/timesync/synchronized"

#: Seconds since the epoch according to the real-time clock hardware, where
#: there is any.
RTC_SINCE_EPOCH = "/sys/class/rtc/rtc0/since_epoch"


class Trust:
    """Whether the clock is trustworthy, and why.

    The reason is carried because it is published: a reader looking at
    cameras running through a low-battery night deserves to see that the
    schedule was suspended rather than guess at a fault.
    """

    def __init__(self, trusted, reason):
        self.trusted = trusted
        self.reason = reason

    def __bool__(self):
        return self.trusted

    def __repr__(self):
        return "Trust(%r, %r)" % (self.trusted, self.reason)


def _synchronised(marker):
    return os.path.exists(marker)


def _rtc_epoch(path):
    try:
        with open(path) as handle:
            return float(handle.read().strip())
    except (OSError, ValueError):
        return None


def assess(high_water=None, marker=SYNCHRONISED_MARKER, rtc=RTC_SINCE_EPOCH):
    """Judge the clock. `high_water` is the latest wall time already seen."""
    if _synchronised(marker):
        return Trust(True, "time synchronised")

    epoch = _rtc_epoch(rtc)
    if epoch is None:
        return Trust(False, "not synchronised and no clock hardware answers")

    if high_water is not None and epoch < float(high_water):
        return Trust(False,
                     "the clock reads earlier than a time already seen, so it "
                     "has been reset or restored")

    return Trust(True, "clock hardware answers and has not gone backwards")
