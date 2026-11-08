"""The Wavlink repair's own record, read as though it were a device.

The repair runs from a timer, not from a collector. It keeps a record of every
attempt so it can hold to its daily limit, and this reads that record so the
attempts reach the database like any other reading: when the last repair was,
how it ended, and how many there were in the last day.

The record is read, never written. A driver may import the driver base and
nothing else, so the few lines that read the file are here rather than taken
from the repair module.
"""

import json
import time

from .base import Driver, DriverError, Point

DAY = 24 * 60 * 60


class WavlinkRepairDriver(Driver):
    """When the Wavlink was last repaired, and how often."""

    description = "the Wavlink repair's attempt record"
    modes = ("poll",)

    def __init__(self, name, params, tags):
        Driver.__init__(self, name, params, tags)
        self.path = self.params.get("path")
        if not self.path:
            raise DriverError(
                "device %r needs a path parameter naming the repair's state "
                "file" % name)
        self._now = time.time

    def _record(self):
        try:
            with open(self.path) as handle:
                record = json.load(handle)
        except FileNotFoundError:
            raise DriverError(
                "%s does not exist, so no repair check has run on this "
                "machine. Check the monitoring-wavlink-recover timer"
                % self.path)
        except (OSError, ValueError) as exc:
            raise DriverError("cannot read %s: %s" % (self.path, exc))
        attempts = record.get("attempts") if isinstance(record, dict) else None
        if not isinstance(attempts, list):
            raise DriverError("%s carries no attempt list" % self.path)
        return record, [a for a in attempts
                        if isinstance(a, dict)
                        and isinstance(a.get("at"), (int, float))
                        and not isinstance(a.get("at"), bool)]

    def read(self):
        record, attempts = self._record()
        now = self._now()
        fields = {
            "attempts_last_day": float(
                sum(1 for a in attempts if now - a["at"] < DAY)),
            "attempts_recorded": float(len(attempts)),
        }
        sightings = record.get("sightings")
        if isinstance(sightings, int) and not isinstance(sightings, bool):
            fields["sightings"] = float(sightings)
        if attempts:
            last = max(attempts, key=lambda a: a["at"])
            fields["last_attempt_at"] = float(last["at"])
            fields["last_result"] = str(last.get("result", ""))
        restored = [a["at"] for a in attempts if a.get("result") == "restored"]
        if restored:
            fields["last_restored_at"] = float(max(restored))
        return [Point(fields)]

    def check(self):
        _, attempts = self._record()
        return "repair record readable, %d attempt(s) in it" % len(attempts)

    def raw(self):
        return self._record()[0]
