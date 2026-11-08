"""The one writer of the switches file.

The command line and the override page both change the operator's switches,
and both do it here. One writer means one answer to what a force looks like,
which expiry it carries, and which zone a local time is read in.

Every change is a read, a change and a write of the whole file. Two writers
doing that at once can each read the old file, and the second write then
drops the first change. So each change holds an exclusive lock on a file next
to the switches file for the whole of the read, the change and the write.
The lock is a separate file because the switches file itself is replaced by a
rename on every write, and a lock on a file that has been renamed away locks
nothing.
"""

import contextlib
import datetime
import fcntl
import math
import os

from monitoring import duration
from monitoring.control import files

STATES = ("on", "off")

#: The forms a local time is read in: the one the command line documents, and
#: the one a browser's date and time field sends.
LOCAL_TIME_FORMATS = ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M")


class OverrideError(ValueError):
    """A change to the switches that is refused, with the reason."""


def _path(path):
    return files.SWITCHES if path is None else path


@contextlib.contextmanager
def _locked(path):
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    # Read only is enough to hold the lock. A lock file made by one user can
    # then still be opened by another, which matters because the command
    # line runs as root and the page does not.
    descriptor = os.open(path + ".lock", os.O_RDONLY | os.O_CREAT, 0o644)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        os.close(descriptor)


def _change(path, change):
    path = _path(path)
    with _locked(path):
        switches = files.read_switches(path)
        files.write_switches(change(switches), path)


def force(name, state, expires_epoch, path=None, known=None):
    """Force one load on or off until a wall time.

    `known` is the set of load names a caller can vouch for. A name outside
    it is refused, so a typo cannot write a force that no load ever reads.
    """
    if state not in STATES:
        raise OverrideError("a force is 'on' or 'off', found %r" % (state,))
    if (isinstance(expires_epoch, bool)
            or not isinstance(expires_epoch, (int, float))
            or not math.isfinite(expires_epoch)):
        # A force with no backstop is the failure this whole design exists to
        # prevent: set during a trip, never cleared, and the battery flat.
        raise OverrideError("a force must carry a finite expiry")
    if known is not None and name not in known:
        raise OverrideError("%r is not a load on this node; the loads are %s"
                            % (name, ", ".join(sorted(known)) or "none"))

    def change(switches):
        loads = dict(switches["loads"])
        loads[name] = {"force": state, "expires_epoch": float(expires_epoch)}
        return dict(switches, loads=loads)
    _change(path, change)


def clear(name, path=None):
    """Remove the force on one load, if it has one."""
    def change(switches):
        loads = dict(switches["loads"])
        loads.pop(name, None)
        return dict(switches, loads=loads)
    _change(path, change)


def set_shedding(enabled, path=None):
    """Let tiers shed loads, or hold every shed load on."""
    _change(path, lambda switches: dict(switches,
                                        shedding_enabled=bool(enabled)))


def expiry_for(text, now):
    """The wall time a duration such as 15m from `now` reaches."""
    try:
        return now + duration.seconds(text, "the duration")
    except duration.DurationError as exc:
        raise OverrideError(str(exc))


def expiry_until(text, zone):
    """The wall time a local time at the site names.

    Read in the site's zone, never the machine's. A Pi left at the default
    zone runs in UTC, hours away from the site.
    """
    for form in LOCAL_TIME_FORMATS:
        try:
            when = datetime.datetime.strptime(text, form)
        except (TypeError, ValueError):
            continue
        return when.replace(tzinfo=zone).timestamp()
    raise OverrideError(
        "a local time is written as \"YYYY-MM-DD HH:MM\", found %r" % (text,))
