"""Checks on the node files that are actually deployed.

These are not about code coverage. They are about the class of mistake that a
unit test never sees: a node file that loads cleanly, lists cleanly, and then
fails on the machine because a driver needs a parameter nobody supplied.

Catching that here means it fails on a laptop in milliseconds rather than at a
site that takes a drive to reach.
"""

import pathlib
import re

import pytest

from monitoring import config, drivers, lineproto, runner
from monitoring.drivers.base import Driver

REPO = pathlib.Path(__file__).resolve().parent.parent
NODE_FILES = sorted((REPO / "sites").glob("*/*.yaml"))
IDS = ["%s/%s" % (p.parent.name, p.name) for p in NODE_FILES]


def load(path):
    return config.load(path, drivers.names())


# -- the registry itself -----------------------------------------------------

@pytest.mark.parametrize("name", sorted(drivers.names()))
def test_every_registered_driver_can_be_imported(name):
    """A typo in the registry is otherwise found by the device never working."""
    cls = drivers.get(name)
    assert issubclass(cls, Driver)


@pytest.mark.parametrize("name", sorted(drivers.names()))
def test_every_driver_declares_a_mode_it_can_work_in(name):
    cls = drivers.get(name)
    assert cls.modes, name
    assert set(cls.modes) <= set(config.MODES), cls.modes


@pytest.mark.parametrize("name", sorted(drivers.names()))
def test_every_driver_describes_itself(name):
    """--list and --check print this while someone is standing at a machine."""
    assert drivers.get(name).description.strip()


# -- the deployed node files -------------------------------------------------

@pytest.mark.parametrize("path", NODE_FILES, ids=IDS)
def test_every_declared_device_can_actually_be_constructed(path):
    """The gap between "the file parses" and "the device works".

    A driver raises for a parameter it cannot do without -- a serial port, a
    one-wire id -- when it is built, not when it is read. Without this, a node
    file missing one of those passes every other check and fails at the site.
    """
    node = load(path)
    for device in node.devices:
        try:
            driver = runner.build_driver(device)
        except Exception as exc:
            pytest.fail("%s: device %r (%s) cannot be built: %s"
                        % (path.name, device.name, device.driver, exc))
        else:
            driver.close()


@pytest.mark.parametrize("path", NODE_FILES, ids=IDS)
def test_every_device_is_in_a_mode_its_driver_supports(path):
    node = load(path)
    for device in node.devices:
        supported = drivers.get(device.driver).modes
        assert device.mode in supported, (
            "%s: %r is %s but %s supports %s"
            % (path.name, device.name, device.mode, device.driver,
               ", ".join(supported)))


@pytest.mark.parametrize("path", NODE_FILES, ids=IDS)
def test_no_two_devices_share_a_tag_set(path):
    """Two devices with identical tags write into one series. If they ever
    share a field name, one value silently overwrites the other, and nothing
    reports it."""
    seen = {}
    for device in node_devices(load(path)):
        key = tuple(sorted(device.tags.items()))
        assert key not in seen, (
            "%s: %r and %r carry identical tags %s. They would share a series."
            % (path.name, seen[key], device.name, dict(key)))
        seen[key] = device.name


def node_devices(node):
    return [d for d in node.devices if d.enabled]


@pytest.mark.parametrize("path", NODE_FILES, ids=IDS)
def test_measurement_and_site_names_are_safe_in_line_protocol(path):
    """A space or comma in a measurement name is legal but has to be escaped
    everywhere it appears, including in queries nobody thinks to escape."""
    node = load(path)
    assert re.match(r"^[A-Za-z0-9_]+$", node.measurement), node.measurement
    for device in node.devices:
        for key, value in device.tags.items():
            assert re.match(r"^[A-Za-z0-9_]+$", str(key)), key
            assert re.match(r"^[A-Za-z0-9_.:-]+$", str(value)), value


# -- what the drivers actually emit ------------------------------------------

FIELD = re.compile(r'^[A-Za-z0-9_]+=(".*"|-?\d+\.\d+)$')


def split_unescaped(text, sep):
    """Split on `sep` where it is neither escaped nor inside a quoted string.

    A regex cannot do this: a field value may legitimately contain the
    separator, as an error message containing a space or a comma does.
    """
    parts, current, quoted, escaped = [], [], False, False
    for char in text:
        if escaped:
            current.append(char)
            escaped = False
        elif char == "\\":
            current.append(char)
            escaped = True
        elif char == '"':
            quoted = not quoted
            current.append(char)
        elif char == sep and not quoted:
            parts.append("".join(current))
            current = []
        else:
            current.append(char)
    parts.append("".join(current))
    return parts


def assert_valid_line(line):
    """Parse a line back, rather than matching substrings of it.

    Substring assertions pass on output that no database would accept. This
    checks the shape: measurement and tags, then fields, then a timestamp,
    with every numeric field a float and every string field quoted.
    """
    parts = split_unescaped(line, " ")
    assert len(parts) == 3, "expected measurement, fields and timestamp: %r" % line
    head, fields_part, timestamp = parts

    assert head, line
    assert timestamp.isdigit(), "no timestamp: %r" % line
    assert fields_part, "no fields: %r" % line

    for tag in split_unescaped(head, ",")[1:]:
        assert "=" in tag, "malformed tag %r in %r" % (tag, line)

    for field in split_unescaped(fields_part, ","):
        assert FIELD.match(field), (
            "field %r is neither a float nor a quoted string, so InfluxDB "
            "would take it as an integer or reject it: %r" % (field, line))


def test_the_validator_rejects_what_it_should():
    """A validator that accepts everything proves nothing about the lines it
    passes, so it is checked against known-bad output first."""
    with pytest.raises(AssertionError):
        assert_valid_line("m count=5 1700000000")        # integer, not a float
    with pytest.raises(AssertionError):
        assert_valid_line("m value=1.0")                 # no timestamp
    with pytest.raises(AssertionError):
        assert_valid_line("m 1700000000")                # no fields
    assert_valid_line("m,a=b value=1.0 1700000000")      # and accepts a good one


def test_every_driver_emits_lines_a_database_would_accept():
    """Every driver that can produce a reading without hardware, checked
    against the shape rather than against a substring."""
    from monitoring.drivers.ds18b20 import Ds18b20Driver
    from monitoring.drivers.dht import DhtDriver
    from monitoring.drivers.host import HostDriver
    from monitoring.drivers import vedirect
    from tests import vedirect_fixtures as fx

    fixtures = pathlib.Path(__file__).resolve().parent / "fixtures"
    cases = [
        ("host", HostDriver("host", {"sysfs_root": str(fixtures / "host"),
                                     "report_address": False}, {}).read()),
        ("ds18b20", Ds18b20Driver("t", {"bus_root": str(fixtures / "w1"),
                                        "device_id": "28-3ce1d44326bf"}, {}).read()),
        ("dht", DhtDriver("box", {"iio_root": str(fixtures / "iio"),
                                  "retries": 0}, {}).read()),
        ("vedirect", vedirect.normalize(dict(fx.SUNNY))),
    ]
    for name, values in cases:
        line = lineproto.line("site", {"location": "x", "node": "n"},
                              dict(values, ok=1.0), 1700000000000000000)
        assert_valid_line(line)


def test_a_tag_value_needing_escapes_survives_the_round_trip():
    """Nothing in the committed files needs this today, which is exactly why
    it is worth pinning before something does."""
    line = lineproto.line("m", {"place": "back shed, north"},
                          {"v": 1.0}, 1700000000000000000)
    assert_valid_line(line)
    # Not line.split(" "): the separator appears inside the value, which is
    # the whole point of escaping it.
    head = split_unescaped(line, " ")[0]
    assert "back\\ shed\\,\\ north" in head
    assert len(split_unescaped(head, ",")) == 2, "the escaped comma split a tag"
