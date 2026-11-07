"""Tomorrow morning's sun, from the Open-Meteo forecast service.

The one driver here whose device is a web service, and it exists so the load
controller never touches the network: a forecast reaches the controller through
the readings cache, like a battery, and ages out of it the same way. A heater
that wants to know whether the morning is worth warming a pack for reads a
number on this machine, and loses nothing but that number when the internet
goes away.

What it publishes
-----------------
``shortwave_wm2``, the mean shortwave radiation over the first few hours after
the next sunrise. Radiation and never cloud cover: a real answer has shown 100
percent cloud with 553 W/m2 reaching the ground, and the panels answer to the
second number.

Each stamp in the answer carries the mean of the hour before it, so an hour
counts as a morning hour when most of it falls after sunrise.

The position
------------
The service has to be told where to forecast, and this repository is public and
the position is not. So it comes from the machine's location file, handed in by
the runner as ``locate`` because a driver reads no files outside its own device,
and it leaves the machine rounded to a tenth of a degree. A tenth is about ten
kilometres, which changes nothing about a forecast.

It is not published, logged or stored, in any form. The service echoes a position
in its answer and can quote one in its errors, a failed request can name its own
URL, and the location code's messages can quote the numbers it refused, so what
goes into a failure here is chosen rather than copied. There is deliberately no
raw mode, since the raw answer is what carries the echo.

Once an hour, and an honest age
-------------------------------
A forecast moves slowly and the service is free, while telegraf runs this every
few minutes in a process that lasts one read. So the time of the last attempt
is kept in a small record in tmpfs, written before the service is called, and
nothing is fetched while it is under an hour old or when it cannot be written:
a record that cannot be kept is a service called on every poll for as long as
the machine runs. A failed attempt waits the hour as well, and every poll in
that hour reports the failure.

A poll between fetches publishes the value it holds, marked ``Unchanged``, so the
database still sees the device every interval and the readings cache is left to
age. Refreshing the cache would make a forecast from a dead link look current
forever, and the age is the only thing a staleness limit has to go on.

The record's stamp is on the monotonic clock, which restarts at a reboot, and
tmpfs empties at the same moment, so the two cannot disagree. The first poll
after a boot fetches, and if the network is not up yet, the next try is an hour
later.
"""

import datetime
import json
import math
import os
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

from ..duration import seconds as _seconds
from .base import Driver, DriverError, Unchanged, run_abandonable

URL = "https://api.open-meteo.com/v1/forecast"

#: The most often this will call the service. A constant and not a parameter,
#: because it is a promise about somebody else's free service.
FETCH_SPACING_SECONDS = 3600.0

#: Beside the readings cache rather than in it: everything in that directory is
#: read as a device's reading. The directory is created at boot and owned by the
#: collector's user, see deploy/tmpfiles-monitoring.conf.
RUNTIME_DIR = "/run/monitoring"

#: Far more than two days of hourly numbers. Exists so a broken proxy cannot
#: fill the memory of a small machine.
MAX_REPLY_BYTES = 256 * 1024

#: The sun at the top of the atmosphere is about 1360. A number past this is not
#: a forecast of anything.
MAX_WM2 = 1500.0

FIELD = "shortwave_wm2"
#: Which morning the number is about. A forecast fetched with a clock a day
#: behind describes the wrong morning and looks fresh for hours, so a reader
#: compares this with the sunrise it is planning for.
SUNRISE_FIELD = "sunrise_epoch"


def https_get(url, timeout):
    """The body the service answers with."""
    request = urllib.request.Request(url,
                                     headers={"User-Agent": "pi-monitoring"})
    with urllib.request.urlopen(request, timeout=timeout) as reply:
        return reply.read(MAX_REPLY_BYTES)


def _moment(text):
    """A local time as the service writes one, or None."""
    if not isinstance(text, str):
        return None
    try:
        return datetime.datetime.fromisoformat(text)
    except ValueError:
        return None


def sunrise_after(document, after):
    """The first sunrise past `after`, naive local time like the answer."""
    try:
        sunrises = document["daily"]["sunrise"]
    except (KeyError, TypeError):
        raise DriverError("the forecast answer is missing radiation or sunrise")
    if not isinstance(sunrises, list):
        raise DriverError("the forecast answer is missing radiation or sunrise")
    ahead = [rise for rise in map(_moment, sunrises)
             if rise is not None and rise > after]
    if not ahead:
        raise DriverError("the forecast answer has no sunrise still to come")
    return min(ahead)


def mean_after_sunrise(document, after, hours):
    """The mean radiation over `hours` hours after the first sunrise past
    `after`, which is naive local time like everything in the answer.

    Anything short of a whole answer raises. A mean over the hours that happened
    to arrive is a smaller number with nothing to say so.
    """
    if not isinstance(document, dict) or document.get("error"):
        raise DriverError("the forecast service answered with no forecast")
    sunrise = sunrise_after(document, after)
    try:
        stamps = document["hourly"]["time"]
        values = document["hourly"]["shortwave_radiation"]
    except (KeyError, TypeError):
        raise DriverError("the forecast answer is missing radiation or sunrise")
    if not all(isinstance(part, list) for part in (stamps, values)):
        raise DriverError("the forecast answer is missing radiation or sunrise")

    half_hour = datetime.timedelta(minutes=30)
    morning = [value for stamp, value in zip(map(_moment, stamps), values)
               if stamp is not None and stamp - half_hour >= sunrise][:hours]
    if len(morning) < hours:
        raise DriverError("the forecast answer stops before the morning ends")
    for value in morning:
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(value) or not 0.0 <= value <= MAX_WM2):
            raise DriverError("the forecast answer has an unusable radiation "
                              "value")
    return float(sum(morning)) / len(morning)


def _failure(exc):
    """What can safely be said about a failed request.

    Chosen rather than copied, since the text of an exception from further down
    can carry the URL, and the URL carries the position.
    """
    if isinstance(exc, urllib.error.HTTPError):
        return "the forecast service answered HTTP %d" % exc.code
    if isinstance(exc, urllib.error.URLError):
        return "cannot reach the forecast service: %s" % (
            type(exc.reason).__name__,)
    return "cannot reach the forecast service: %s" % type(exc).__name__


class OpenMeteoDriver(Driver):
    """The morning's forecast radiation, for a load that plans around it."""

    description = "forecast radiation after sunrise, from Open-Meteo"

    # A fetch can take its whole timeout, so it does not share a process and a
    # time limit with the sensors that answer at once.
    slow_read = True
    needs_location = True
    # The record is what makes it once an hour, and it works one read at a time.
    modes = ("poll",)

    def __init__(self, name, params, tags):
        Driver.__init__(self, name, params, tags)
        for key in ("latitude", "longitude"):
            if key in self.params:
                raise DriverError(
                    "device %r must not carry a %s. Node files are public: "
                    "the position belongs in /etc/monitoring/location.yaml "
                    "and is read from there" % (name, key))
        self.hours = self.params.get("hours", 4)
        if (isinstance(self.hours, bool) or not isinstance(self.hours, int)
                or not 1 <= self.hours <= 24):
            raise DriverError("device %r hours must be a whole number from 1 "
                              "to 24, found %r" % (name, self.hours))
        self.timeout = _seconds(self.params.get("timeout", 10),
                                "device %r timeout" % name)
        self.state_file = self.params.get(
            "state_file", os.path.join(RUNTIME_DIR, "forecast-%s.json" % name))
        # Supplied by the runner, or by a test along with the others here.
        self._locate = self.params.get("locate")
        self._fetch = self.params.get("fetch", https_get)
        self._monotonic = self.params.get("monotonic", time.monotonic)
        self._now = self.params.get("now", datetime.datetime.now)

    # -- the question -------------------------------------------------------

    def _place(self):
        """The machine's position, with any failure's own words left out,
        because the location code's messages can quote the numbers."""
        try:
            place = self._locate()
            latitude = float(place["latitude"])
            longitude = float(place["longitude"])
            zone = place["zone"]
        except Exception as exc:
            raise DriverError(
                "the machine's location file will not load (%s), so no "
                "forecast can be asked for. Its full message can quote a "
                "position, so it is not repeated here; running the "
                "controller's dry-run prints it" % type(exc).__name__)
        return latitude, longitude, zone

    def url(self):
        """The whole request. Never to be written anywhere."""
        latitude, longitude, zone = self._place()
        return "%s?%s" % (URL, urllib.parse.urlencode((
            ("latitude", "%.1f" % latitude),
            ("longitude", "%.1f" % longitude),
            ("hourly", "shortwave_radiation"),
            ("daily", "sunrise"),
            ("timezone", zone.key),
            ("forecast_days", 2))))

    def _ask(self, url):
        """One fetch, turned into the published number.

        Runs on a thread this will walk away from, because a socket timeout
        does not cover a name lookup and telegraf's limit covers every device
        in the process.
        """
        zone = self._place()[2]
        try:
            body = run_abandonable(
                "the forecast service", self.timeout,
                lambda: self._fetch(url, self.timeout))
        except DriverError:
            raise
        except Exception as exc:
            raise DriverError(_failure(exc))
        try:
            document = json.loads(body)
        except ValueError:
            raise DriverError("the forecast service answered with something "
                              "that is not JSON")
        after = self._now(zone).replace(tzinfo=None)
        value = mean_after_sunrise(document, after, self.hours)
        sunrise = sunrise_after(document, after).replace(tzinfo=zone)
        return {FIELD: value, SUNRISE_FIELD: sunrise.timestamp()}

    # -- the record ---------------------------------------------------------

    def _recorded(self):
        """The last attempt if it is still inside the hour, or None.

        A record that cannot be believed means fetching. The write that follows
        replaces it, so it costs one call rather than a call every poll.
        """
        try:
            with open(self.state_file) as handle:
                record = json.load(handle)
        except (OSError, ValueError):
            return None
        at = record.get("at") if isinstance(record, dict) else None
        if isinstance(at, bool) or not isinstance(at, (int, float)):
            return None
        if not 0.0 <= self._monotonic() - at < FETCH_SPACING_SECONDS:
            return None
        return record

    def _record(self, at, **outcome):
        """Rewrite the record whole, so a kill leaves the old one or this."""
        directory = os.path.dirname(self.state_file) or "."
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                    mode="w", dir=directory, delete=False,
                    prefix=".forecast-") as handle:
                temporary = handle.name
                json.dump(dict(outcome, at=at), handle)
            # Created 0600. A hand run as root would otherwise leave a record
            # the collector's user cannot read.
            os.chmod(temporary, 0o644)
            os.replace(temporary, self.state_file)
        except OSError as exc:
            if temporary is not None:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass
            raise DriverError(
                "cannot write %s (%s), so nothing limits how often the "
                "forecast service is called, and it is not being called"
                % (self.state_file, type(exc).__name__))

    # -- the driver ---------------------------------------------------------

    def read(self):
        recorded = self._recorded()
        if recorded is not None:
            if recorded.get("error"):
                raise DriverError(recorded["error"])
            value = recorded.get("value")
            sunrise = recorded.get("sunrise")
            if all(isinstance(number, (int, float))
                   and not isinstance(number, bool)
                   for number in (value, sunrise)):
                return Unchanged({FIELD: float(value),
                                  SUNRISE_FIELD: float(sunrise)})
            raise DriverError("the last fetch of the forecast did not finish")

        # Before the record, so a location file still being written does not
        # use up the hour.
        url = self.url()
        at = self._monotonic()
        self._record(at, error="the last fetch of the forecast did not finish")
        try:
            fields = self._ask(url)
        except DriverError as exc:
            self._record(at, error=str(exc))
            raise
        self._record(at, value=fields[FIELD], sunrise=fields[SUNRISE_FIELD])
        return fields

    def read_once(self):
        """One fetch outside the record, as a read would publish it."""
        return self._ask(self.url())

    def check(self):
        """Call the service once, outside the record. A person is watching, and
        a check that used up the hour would hold back the first real reading."""
        return "forecast service answering, %.0f W/m2 after the next sunrise" % (
            self.read_once()[FIELD],)
