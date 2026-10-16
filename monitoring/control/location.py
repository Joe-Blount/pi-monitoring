"""Where the machine is, read from a file that is not in this repository.

A solar schedule needs a place and a timezone. Latitude sets how long the day
is; longitude sets what time solar noon happens, which is what turns a day
length into clock times. Without the second, the arithmetic has to assume the
machine sits on its timezone's central meridian, and this one is about seven
degrees off it, which is half an hour of error in every sunrise. For windows
built around legal shooting light, half an hour is the whole feature.

Why it is not in the node file
------------------------------
The node files are committed, and this repository is public. A node file is
otherwise the right home for a property of the machine, and that is where
this started, but it would publish the owner's coordinates to anyone who
reads the repository.

So it lives beside the credentials instead, in a file the install script
creates and nothing commits, and the repository carries only an example.
``location.yaml.example`` holds a nearby town rounded to a tenth of a degree,
which is about ten kilometres of fuzz and under a minute of error, so even
the example gives away nothing and still demonstrates the shape.

Refused, never defaulted
------------------------
A missing or half-written file raises. There is no sensible default: a
schedule computed against an invented point is wrong in a way nothing
downstream can detect, and it shows up as cameras keeping a schedule nobody
asked for, months later, on a machine nobody is standing next to.
"""

import os

#: Beside the credentials, for the same reason: it is per machine, it is not
#: in the repository, and the install script puts it there.
DEFAULT_PATH = "/etc/monitoring/location.yaml"

#: Lets a hand run or a test say otherwise, the same way MONITORING_NODE
#: already does for the node file.
PATH_VAR = "MONITORING_LOCATION"

#: Plausible bounds, not precision. They catch a swapped pair, which is the
#: mistake that looks most like a working value: this site's longitude read
#: as a latitude is a place in the Arctic Ocean, and the schedule it produces
#: is wrong rather than absent.
LATITUDE_RANGE = (-90.0, 90.0)
LONGITUDE_RANGE = (-180.0, 180.0)


class LocationError(ValueError):
    """The machine's place cannot be read, so no schedule can be placed."""


def path():
    return os.environ.get(PATH_VAR) or DEFAULT_PATH


def read(where=None):
    """Return latitude, longitude and timezone, or raise saying what to fix.

    The message is written for somebody standing at the machine with the
    file in front of them, because that is who reads it.
    """
    where = where or path()
    try:
        import yaml
    except ImportError as exc:                      # pragma: no cover
        raise LocationError("reading %s needs python3-yaml: %s"
                            % (where, exc))

    try:
        with open(where) as handle:
            raw = yaml.safe_load(handle)
    except FileNotFoundError:
        raise LocationError(
            "%s does not exist, so no solar schedule can be placed. Copy "
            "location.yaml.example from this repository to that path and put "
            "the machine's own latitude, longitude and timezone in it. It is "
            "deliberately not in the repository, so it has to be written once "
            "per machine." % where)
    except OSError as exc:
        raise LocationError("cannot read %s: %s" % (where, exc))
    except yaml.YAMLError as exc:
        raise LocationError("%s is not valid YAML: %s" % (where, exc))

    if not isinstance(raw, dict):
        raise LocationError(
            "%s should be a mapping with latitude, longitude and timezone"
            % where)

    unknown = set(raw) - {"latitude", "longitude", "timezone"}
    if unknown:
        # The same rule the control spec applies: a key nobody reads sits in
        # the file looking like it is in force.
        raise LocationError(
            "%s has unknown key(s) %s; it takes latitude, longitude and "
            "timezone" % (where, ", ".join(sorted(unknown))))

    return {
        "latitude": _degrees(where, raw, "latitude", LATITUDE_RANGE),
        "longitude": _degrees(where, raw, "longitude", LONGITUDE_RANGE),
        "zone": _zone(where, raw.get("timezone")),
    }


def _degrees(where, raw, name, bounds):
    if name not in raw:
        raise LocationError("%s needs a %s" % (where, name))
    value = raw[name]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise LocationError(
            "%s: %s must be a number in degrees, found %r. Degrees and "
            "decimals, not degrees and minutes: 37 degrees 2 minutes north "
            "is 37.05, not 37.02" % (where, name, value))
    value = float(value)
    low, high = bounds
    if not low <= value <= high:
        raise LocationError(
            "%s: %s of %g is outside %g to %g" % (where, name, value, low,
                                                  high))
    return value


def _zone(where, name):
    import zoneinfo

    if not isinstance(name, str) or not name:
        raise LocationError(
            "%s needs a timezone naming an IANA zone, such as "
            "America/Chicago. An offset will not do: the schedules have to "
            "follow daylight saving, and two days a year an offset is an "
            "hour wrong" % where)
    try:
        return zoneinfo.ZoneInfo(name)
    except Exception as exc:
        raise LocationError(
            "%s: timezone %r is not a zone this machine knows: %s"
            % (where, name, exc))
