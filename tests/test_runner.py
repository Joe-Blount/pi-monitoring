import io
import pathlib

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
    assert result.lines == ["m,device=a ok=1.0,value=2.0 %d" % NOW]


def test_calibration_is_applied_to_the_reading(tmp_path, doubles):
    node = node_with(tmp_path, """  - name: a
    driver: good
    mode: poll
    calibration: {value: {scale: 10.0, offset: 1.0}}
""")
    result = runner.read_device(node, node.find("a"), NOW)
    assert result.lines == ["m,device=a ok=1.0,value=21.0 %d" % NOW]


def test_a_disabled_device_publishes_that_it_is_disabled(tmp_path, doubles):
    """Every declared device publishes something, so silence always means
    failure rather than a decision somebody made months ago."""
    node = node_with(tmp_path, "  - {name: a, driver: good, mode: poll, enabled: false}\n")
    result = runner.read_device(node, node.find("a"), NOW)
    assert result.ok and result.skipped
    assert result.lines == ["m,device=a enabled=0.0 %d" % NOW]


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
    written = runner.poll(node, NOW, out, err)
    assert written == 3, "two readings and one failure point"
    assert out.getvalue().count("value=2.0") == 2
    assert "device b (bad): the sensor is not there" in err.getvalue()

    # The failure is published as well as logged. A log on a machine at the
    # end of a track is very nearly as silent as nothing.
    assert 'ok=0.0' in out.getvalue()
    assert 'error_message="the sensor is not there"' in out.getvalue()


def test_a_long_failure_reason_is_shortened_for_the_log_but_not_the_point(
        tmp_path, doubles):
    """telegraf truncates a command's stderr at 512 bytes in total, so several
    long messages push each other out. A published field has no such limit."""
    node = node_with(tmp_path, "  - {name: a, driver: bad, mode: poll}\n")
    long_reason = "x" * 400
    out, err = io.StringIO(), io.StringIO()

    import monitoring.runner as mod
    original = mod.read_device
    mod.read_device = lambda n, d, ns=None: runner.Result(d, error=long_reason)
    try:
        runner.poll(node, NOW, out, err)
    finally:
        mod.read_device = original

    assert len(err.getvalue().strip()) < 250
    assert long_reason in out.getvalue()





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


class ExplodingMidStreamDriver(Driver):
    description = "test double with a bug partway through a stream"

    def stream(self):
        yield {"value": 1.0}
        raise ZeroDivisionError("a bug, not a cable")


class EmptyReadingDriver(Driver):
    description = "test double whose reading has nothing in it"

    def read(self):
        return {"everything": None}


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
        "m,device=a ok=1.0,value=1.0 1700000000",
        "m,device=a ok=1.0,value=2.0 1700000000",
        "m,device=a ok=1.0,value=3.0 1700000000"]


def test_a_device_that_goes_away_mid_stream_exits_non_zero(tmp_path, stream_doubles):
    """telegraf restarts an execd child after its restart delay, so failing
    out is the retry, and writing one here would duplicate it."""
    node = node_with(tmp_path, "  - {name: a, driver: breaker, mode: resident}\n")
    out, err = io.StringIO(), io.StringIO()
    code = runner.stream_device(node, node.find("a"), out, err, clock=frozen_clock)
    assert code == 1
    assert "the cable was unplugged" in err.getvalue()
    assert out.getvalue().strip() == "m,device=a ok=1.0,value=1.0 1700000000"


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
        "m,device=a enabled=0.0 1700000000", "m,device=a enabled=0.0 1700000000"]
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
    assert out.getvalue().strip() == "m,device=a ok=1.0,value=101.0 1700000000"


# -- devices nothing will collect --------------------------------------------

def controller_node(tmp_path, control_enabled):   # noqa: needs the doubles
    path = tmp_path / "node.yaml"
    path.write_text("""
site: s
node: n
measurement: m
devices:
  - {name: mppt, driver: vedirect, mode: controller, params: {port: /dev/x}}
  - {name: counter, driver: good, mode: poll}
control:
  enabled: %s
  loads:
    cameras: {control_pin: 23}
""" % ("true" if control_enabled else "false"))
    return config.load(path, set(REGISTRY))


def test_a_controller_device_with_control_disabled_is_reported_as_stranded(tmp_path, doubles):
    """Nothing reads it, and that absence looks exactly like a sensor that is
    working and reporting nothing."""
    node = controller_node(tmp_path, control_enabled=False)
    assert [d.name for d in runner.orphaned(node)] == ["mppt"]


def test_a_controller_device_with_control_enabled_is_not_stranded(tmp_path, doubles):
    node = controller_node(tmp_path, control_enabled=True)
    assert runner.orphaned(node) == []


def test_listing_marks_a_stranded_device(tmp_path, doubles):
    out = io.StringIO()
    runner.listing(controller_node(tmp_path, control_enabled=False), out)
    assert "NOT COLLECTED" in out.getvalue()


def test_check_fails_a_stranded_device_rather_than_probing_it(tmp_path, doubles):
    """Probing it would report on hardware that nothing is going to read,
    which is a confusing kind of success."""
    out = io.StringIO()
    failures = runner.check(controller_node(tmp_path, control_enabled=False), out)
    text = out.getvalue()

    assert "FAIL  mppt" in text
    assert "nothing will read it" in text
    # It must not have been probed: the port does not exist, and an error
    # about opening it would send someone looking at the wiring instead of at
    # the configuration.
    assert "/dev/x" not in text
    # Exactly one. The other device is a double that always passes, so this
    # number no longer depends on whether the machine running the tests has a
    # /proc to read.
    assert failures == 1


def test_poll_still_collects_while_another_device_is_stranded(tmp_path, doubles):
    """The previous version of this asserted a list comprehension in the
    config loader and never called poll at all."""
    node = controller_node(tmp_path, control_enabled=False)
    out, err = io.StringIO(), io.StringIO()
    written = runner.poll(node, NOW, out, err)
    assert written == 1
    assert "value=2.0" in out.getvalue()
    assert "mppt" not in out.getvalue(), "a stranded device must not be polled"


def test_streaming_refuses_a_device_the_controller_owns(tmp_path, stream_doubles):
    """Streaming it here would fight the controller for the port."""
    import subprocess
    import sys as _sys
    path = tmp_path / "node.yaml"
    path.write_text("""
site: s
node: n
measurement: m
devices:
  - {name: mppt, driver: vedirect, mode: controller, params: {port: /dev/x}}
control:
  enabled: true
  loads:
    cameras: {control_pin: 23}
""")
    repo = pathlib.Path(__file__).resolve().parent.parent
    out = subprocess.run(
        [_sys.executable, str(repo / "bin" / "collect"), "mppt",
         "--node", str(path), "--stream"],
        capture_output=True, text=True, timeout=60)
    assert out.returncode == 2
    assert "controller owns" in out.stderr


def test_a_driver_that_crashes_mid_stream_is_named_not_swallowed(
        tmp_path, stream_doubles):
    """A bug in a driver must not look like a dead sensor, and must end the
    process so telegraf restarts it."""
    REGISTRY["exploder"] = ("tests.test_runner", "ExplodingMidStreamDriver")
    try:
        node = node_with(tmp_path, "  - {name: a, driver: exploder, mode: resident}\n")
        err = io.StringIO()
        code = runner.stream_device(node, node.find("a"), io.StringIO(), err,
                                    clock=frozen_clock)
        assert code == 1
        assert "ZeroDivisionError" in err.getvalue()
    finally:
        REGISTRY.pop("exploder", None)


def test_a_driver_returning_nothing_usable_is_reported_not_crashed(
        tmp_path, doubles):
    """A reading with no usable fields cannot be written. That must surface as
    a device error, not as a traceback that takes the process down."""
    REGISTRY["empty"] = ("tests.test_runner", "EmptyReadingDriver")
    try:
        node = node_with(tmp_path, "  - {name: a, driver: empty, mode: poll}\n")
        result = runner.read_device(node, node.find("a"), NOW)
        assert not result.ok
        assert "no usable values" in result.error
    finally:
        REGISTRY.pop("empty", None)


def test_a_failure_point_counts_as_something_written(tmp_path, doubles):
    """The count decides the exit code, and telegraf discards a command's
    stdout entirely when that is non-zero. If failure points did not count,
    the case where every device is down would throw away the very points that
    exist to report it."""
    node = node_with(tmp_path, "  - {name: a, driver: bad, mode: poll}\n")
    out, err = io.StringIO(), io.StringIO()
    written = runner.poll(node, NOW, out, err)
    assert written == 1
    assert "ok=0.0" in out.getvalue()


class CountingStream(io.StringIO):
    """Records how often it was flushed, which is the thing being asserted."""

    def __init__(self):
        io.StringIO.__init__(self)
        self.flushes = 0

    def flush(self):
        self.flushes += 1


def test_streaming_flushes_after_every_line(tmp_path, stream_doubles):
    """Under execd, stdout is a pipe and Python buffers it at 8 KiB. At one
    short line a minute that is about ninety minutes of latency, so a tip at
    the end of a storm would arrive after the storm."""
    node = node_with(tmp_path, "  - {name: a, driver: streamer, mode: resident}\n")
    out = CountingStream()
    runner.stream_device(node, node.find("a"), out, io.StringIO(),
                         max_readings=3, clock=frozen_clock)
    assert out.flushes >= 3, (
        "flushed %d times for 3 lines; readings would arrive in clumps"
        % out.flushes)


def test_the_poll_command_exits_zero_when_every_device_fails(tmp_path):
    """Run as telegraf runs it. A non-zero exit makes telegraf discard the
    output, so the failure points would never reach the dashboard."""
    import subprocess
    import sys as _sys
    path = tmp_path / "node.yaml"
    path.write_text("""
site: s
node: n
measurement: m
devices:
  - {name: a, driver: ds18b20, mode: poll, params: {device_id: 28-nope, bus_root: /nonexistent}}
""")
    repo = pathlib.Path(__file__).resolve().parent.parent
    out = subprocess.run(
        [_sys.executable, str(repo / "bin" / "collect"), "--node", str(path), "--poll"],
        capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, "telegraf would discard stdout on a non-zero exit"
    assert "ok=0.0" in out.stdout, "the failure must still be published"
    assert "28-nope" in out.stderr, "and still logged"
