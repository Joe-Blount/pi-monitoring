import io

import pytest

from monitoring import config, runner
from monitoring.drivers import REGISTRY
from monitoring.drivers.base import Driver, DriverError

NOW = 1700000000000000000


class GoodDriver(Driver):
    description = "test double that always reads"

    def read(self):
        return {"value": 2.0}

    def check(self):
        return "always fine"


class BadDriver(Driver):
    description = "test double that never reads"

    def read(self):
        raise DriverError("the sensor is not there")

    def check(self):
        raise DriverError("the sensor is not there")


class ExplodingDriver(Driver):
    description = "test double with a bug in it"

    def read(self):
        raise ZeroDivisionError("a bug, not a dead sensor")


@pytest.fixture
def doubles():
    """Register test doubles as drivers, then put the registry back."""
    original = dict(REGISTRY)
    REGISTRY["good"] = ("tests.test_runner", "GoodDriver")
    REGISTRY["bad"] = ("tests.test_runner", "BadDriver")
    REGISTRY["boom"] = ("tests.test_runner", "ExplodingDriver")
    yield
    REGISTRY.clear()
    REGISTRY.update(original)


def node_with(tmp_path, body):
    path = tmp_path / "node.yaml"
    path.write_text("site: s\nnode: n\nmeasurement: m\ndevices:\n" + body)
    return config.load(path, set(REGISTRY))


def test_a_reading_becomes_a_point(tmp_path, doubles):
    node = node_with(tmp_path, "  - {name: a, driver: good, mode: poll}\n")
    result = runner.read_device(node, node.find("a"), NOW)
    assert result.ok
    assert result.lines == ["m value=2.0 %d" % NOW]


def test_calibration_is_applied_to_the_reading(tmp_path, doubles):
    node = node_with(tmp_path, """  - name: a
    driver: good
    mode: poll
    calibration: {value: {scale: 10.0, offset: 1.0}}
""")
    result = runner.read_device(node, node.find("a"), NOW)
    assert result.lines == ["m value=21.0 %d" % NOW]


def test_a_disabled_device_publishes_that_it_is_disabled(tmp_path, doubles):
    """Every declared device publishes something, so silence always means
    failure rather than a decision somebody made months ago."""
    node = node_with(tmp_path, "  - {name: a, driver: good, mode: poll, enabled: false}\n")
    result = runner.read_device(node, node.find("a"), NOW)
    assert result.ok and result.skipped
    assert result.lines == ["m enabled=0.0 %d" % NOW]


def test_a_dead_sensor_reports_an_error_rather_than_raising(tmp_path, doubles):
    node = node_with(tmp_path, "  - {name: a, driver: bad, mode: poll}\n")
    result = runner.read_device(node, node.find("a"), NOW)
    assert not result.ok and "not there" in result.error


def test_a_driver_bug_is_caught_and_named(tmp_path, doubles):
    """A crash in a driver must not look like a dead sensor, and must not
    take the process down with it."""
    node = node_with(tmp_path, "  - {name: a, driver: boom, mode: poll}\n")
    result = runner.read_device(node, node.find("a"), NOW)
    assert not result.ok and "ZeroDivisionError" in result.error


def test_one_dead_sensor_does_not_cost_the_others(tmp_path, doubles):
    node = node_with(tmp_path, """  - {name: a, driver: good, mode: poll}
  - {name: b, driver: bad, mode: poll}
  - {name: c, driver: good, mode: poll}
""")
    out, err = io.StringIO(), io.StringIO()
    failures = runner.poll(node, NOW, out, err)
    assert failures == 1
    assert out.getvalue().count("value=2.0") == 2
    assert "device b (bad): the sensor is not there" in err.getvalue()


def test_poll_ignores_devices_that_are_not_poll_mode(tmp_path, doubles):
    node = node_with(tmp_path, """  - {name: a, driver: good, mode: poll}
  - {name: b, driver: good, mode: resident}
  - {name: c, driver: good, mode: controller}
""")
    out = io.StringIO()
    runner.poll(node, NOW, out, io.StringIO())
    assert len(out.getvalue().strip().splitlines()) == 1


def test_check_reports_each_device_and_counts_failures(tmp_path, doubles):
    node = node_with(tmp_path, """  - {name: a, driver: good, mode: poll}
  - {name: b, driver: bad, mode: poll}
  - {name: c, driver: good, mode: poll, enabled: false}
""")
    out = io.StringIO()
    failures = runner.check(node, out)
    text = out.getvalue()
    assert failures == 1
    assert "ok    a" in text and "FAIL  b" in text and "skip  c" in text


def test_listing_shows_disabled_state(tmp_path, doubles):
    node = node_with(tmp_path, "  - {name: a, driver: good, mode: poll, enabled: false}\n")
    out = io.StringIO()
    runner.listing(node, out)
    assert "[disabled]" in out.getvalue()


# -- resident mode -----------------------------------------------------------

class StreamingDriver(Driver):
    description = "test double that streams"

    def stream(self):
        for value in (1.0, 2.0, 3.0, 4.0):
            yield {"value": value}


class FailingStreamDriver(Driver):
    description = "test double whose device goes away mid-stream"

    def stream(self):
        yield {"value": 1.0}
        raise DriverError("the cable was unplugged")


class EndingStreamDriver(Driver):
    description = "test double whose device simply stops"

    def stream(self):
        yield {"value": 1.0}


@pytest.fixture
def stream_doubles():
    original = dict(REGISTRY)
    REGISTRY["streamer"] = ("tests.test_runner", "StreamingDriver")
    REGISTRY["breaker"] = ("tests.test_runner", "FailingStreamDriver")
    REGISTRY["ender"] = ("tests.test_runner", "EndingStreamDriver")
    yield
    REGISTRY.clear()
    REGISTRY.update(original)


def frozen_clock():
    return 1.7


def test_streaming_prints_a_line_per_reading(tmp_path, stream_doubles):
    node = node_with(tmp_path, "  - {name: a, driver: streamer, mode: resident}\n")
    out = io.StringIO()
    code = runner.stream_device(node, node.find("a"), out, io.StringIO(),
                                max_readings=3, clock=frozen_clock)
    assert code == 0
    assert out.getvalue().splitlines() == [
        "m value=1.0 1700000000", "m value=2.0 1700000000", "m value=3.0 1700000000"]


def test_a_device_that_goes_away_mid_stream_exits_non_zero(tmp_path, stream_doubles):
    """telegraf restarts an execd child after its restart delay, so failing
    out is the retry, and writing one here would duplicate it."""
    node = node_with(tmp_path, "  - {name: a, driver: breaker, mode: resident}\n")
    out, err = io.StringIO(), io.StringIO()
    code = runner.stream_device(node, node.find("a"), out, err, clock=frozen_clock)
    assert code == 1
    assert "the cable was unplugged" in err.getvalue()
    assert out.getvalue().strip() == "m value=1.0 1700000000"


def test_a_stream_that_simply_ends_is_also_a_failure(tmp_path, stream_doubles):
    node = node_with(tmp_path, "  - {name: a, driver: ender, mode: resident}\n")
    err = io.StringIO()
    code = runner.stream_device(node, node.find("a"), io.StringIO(), err,
                                clock=frozen_clock)
    assert code == 1
    assert "stopped sending" in err.getvalue()


def test_a_disabled_resident_device_keeps_reporting_that_it_is_disabled(
        tmp_path, stream_doubles):
    """Otherwise a disabled resident device would be indistinguishable from a
    dead one, which is exactly the confusion the rule exists to prevent."""
    node = node_with(tmp_path,
                     "  - {name: a, driver: streamer, mode: resident, enabled: false}\n")
    out = io.StringIO()
    slept = []
    code = runner.stream_device(node, node.find("a"), out, io.StringIO(),
                                max_readings=2, sleep=slept.append, clock=frozen_clock)
    assert code == 0
    assert out.getvalue().splitlines() == [
        "m enabled=0.0 1700000000", "m enabled=0.0 1700000000"]
    assert slept == [30.0]


def test_calibration_applies_to_streamed_readings_too(tmp_path, stream_doubles):
    node = node_with(tmp_path, """  - name: a
    driver: streamer
    mode: resident
    calibration: {value: {offset: 100.0}}
""")
    out = io.StringIO()
    runner.stream_device(node, node.find("a"), out, io.StringIO(),
                         max_readings=1, clock=frozen_clock)
    assert out.getvalue().strip() == "m value=101.0 1700000000"
