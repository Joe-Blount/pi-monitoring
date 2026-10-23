"""The control loop, read as though it were a device.

The loop is a separate service, not a collector, but what it decided has to
reach the database the same way everything else does. Rather than give it a
second output path with its own credentials and its own retry, it writes a
small document and this reads it. The node file declares a device like any
other, telegraf's existing poll fragment collects it, and nothing new appears
in the telegraf configuration.

Reading the loop rather than asking it also means this cannot interfere with
it. A driver that called into the control logic to find out what it thought
would be a second opinion rather than a report.

Why this file duplicates three small helpers
--------------------------------------------
A driver in this project may import the driver base and nothing else, so that
any of them can be lifted into its own package by moving one file. Reaching
into the control package for a JSON reader would break that for the sake of
about a dozen lines, so the lines are here instead. The path is not here
either: it comes from the node file, and a test holds the node file and the
loop to the same value, which is a better place for that agreement than an
import that would cost the boundary.

Why a missing document is a failure
-----------------------------------
If the loop is not running this raises, which publishes ok=0 with the reason
exactly as a dead sensor does. That is the point: a controller that has
stopped is the most important thing on the node to know about, because every
load is sitting at its fail-safe and nothing is managing the battery. Quietly
publishing nothing would leave a gap that reads like a collector problem.

The age of the document is computed here, against this process's own clock,
rather than copied out of the file. A loop that has wedged with its last
decision in place would otherwise look healthy: every field would hold its
final value and nothing would say when it stopped.

One point per load
------------------
The loop decides each load on its own, and this reports each one as its own
point tagged with the load, with the field names bare. A chart then repeats
over loads and a table over all of them is generic, so a load added to the
node file needs no panel edited. The loop's own fields, and the age, go on
the point with no load tag. Every load the loop drives is in the document it
writes, so none can be driven and go unreported.
"""

import json
import os
import time

from .base import Driver, DriverError, Point

#: Where the kernel names this boot. The loop stamps its document with it, and
#: a stamp from a previous boot cannot be aged, because the monotonic clock
#: those stamps are on restarts at zero.
BOOT_ID_PATH = "/proc/sys/kernel/random/boot_id"


def _boot_id():
    try:
        with open(BOOT_ID_PATH) as handle:
            return handle.read().strip() or None
    except OSError:
        return None


def _monotonic():
    return time.clock_gettime(time.CLOCK_MONOTONIC)


def _read_json(path):
    try:
        with open(path) as handle:
            value = json.load(handle)
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _values(fields):
    """The fields a point can carry: numbers and text, and not a boolean,
    which the line protocol would otherwise write as one."""
    return {name: value for name, value in fields.items()
            if isinstance(value, str)
            or (isinstance(value, (int, float))
                and not isinstance(value, bool))}


class ControlDriver(Driver):
    """What the load controller last decided, and how long ago."""

    description = "the load controller's own state"
    modes = ("poll",)

    def __init__(self, name, params, tags):
        Driver.__init__(self, name, params, tags)
        self.path = self.params.get("path")
        if not self.path:
            raise DriverError(
                "device %r needs a path parameter naming the file the load "
                "controller publishes" % name)
        self._monotonic = _monotonic
        self._boot_id = _boot_id

    def _document(self):
        record = _read_json(self.path)
        if not record:
            raise DriverError(
                "the load controller has published nothing at %s. If it is "
                "not running then every load is sitting at its fail-safe and "
                "nothing is managing the battery: check the service"
                % self.path)
        fields = record.get("fields")
        if not isinstance(fields, dict) or not fields:
            raise DriverError(
                "%s carries no fields, so the controller wrote a document it "
                "could not fill" % self.path)
        loads = record.get("loads")
        if not isinstance(loads, dict):
            # Empty is allowed: a node file with every load disabled is a
            # controller with nothing to drive, and its own fields still say
            # it is alive. Absent is not, because a load driven and not
            # reported is the failure this device exists to prevent.
            raise DriverError(
                "%s carries no loads. A controller still running code from "
                "before the last install writes that shape: restart "
                "monitoring-control" % self.path)
        for name, load in loads.items():
            if not isinstance(load, dict) or not load:
                raise DriverError(
                    "%s carries no fields for load %s, so the controller "
                    "wrote a document it could not fill" % (self.path, name))
        return record, fields, loads

    def read(self):
        record, fields, loads = self._document()

        stamp = record.get("monotonic")
        boot = self._boot_id()
        if not boot or record.get("boot_id") != boot or not isinstance(
                stamp, (int, float)) or isinstance(stamp, bool):
            # A document from a previous boot cannot be aged, and saying so is
            # better than guessing. The loop rewrites it within one pass of
            # starting, so this is a brief state rather than a lasting one.
            raise DriverError(
                "%s was not written during this boot, so the controller has "
                "not published since the machine started" % self.path)

        own = _values(fields)
        own["published_age_seconds"] = max(
            0.0, self._monotonic() - float(stamp))
        points = [Point(own)]
        for name in sorted(loads):
            values = _values(loads[name])
            if not values:
                raise DriverError(
                    "%s carries no usable value for load %s, so the "
                    "controller wrote a document it could not fill"
                    % (self.path, name))
            points.append(Point(values, tags={"load": name}))
        return points

    def check(self):
        _, _, loads = self._document()
        return "controller published %s" % (
            ", ".join("load %s" % name for name in sorted(loads))
            or "no load")

    def raw(self):
        return _read_json(self.path)
