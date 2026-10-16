import io
import pathlib

import pytest

from monitoring import config, runner
from monitoring.control import files as control_files
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


def test_streaming_refuses_a_device_that_is_not_resident(tmp_path, stream_doubles):
    """Streaming a poll device would run a second reader alongside the one
    telegraf already schedules."""
    import subprocess
    import sys as _sys
    path = tmp_path / "node.yaml"
    path.write_text("""
site: s
node: n
measurement: m
devices:
  - {name: bms, driver: jbd, mode: poll, params: {address: "AA:BB:CC:DD:EE:FF"}}
""")
    repo = pathlib.Path(__file__).resolve().parent.parent
    out = subprocess.run(
        [_sys.executable, str(repo / "bin" / "collect"), "bms",
         "--node", str(path), "--stream"],
        capture_output=True, text=True, timeout=60)
    assert out.returncode == 2
    assert "not resident" in out.stderr
    assert "--poll" in out.stderr


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


class FlushCounter(io.StringIO):
    """Records how often it was flushed, which is the behaviour under test."""

    def __init__(self):
        io.StringIO.__init__(self)
        self.flushes = 0

    def flush(self):
        self.flushes += 1
        io.StringIO.flush(self)


def test_poll_flushes_after_each_device(tmp_path, doubles):
    """Standard output to a pipe is block buffered. Flushing only at the end
    means a process killed on timeout while reading a later device loses every
    reading already taken, including the points reporting the failures. That
    turns one slow device into silence for the whole node."""
    node = node_with(tmp_path,
                     "  - {name: a, driver: good, mode: poll}\n"
                     "  - {name: b, driver: good, mode: poll}\n"
                     "  - {name: c, driver: bad, mode: poll}\n")
    out = FlushCounter()
    written = runner.poll(node, out=out, err=io.StringIO())
    assert written >= 2
    assert out.flushes >= written, (
        "flushed %d times for %d devices" % (out.flushes, written))


# --- a resident device is owned by its collector --------------------------

def test_a_resident_device_held_by_its_collector_is_not_a_failure(tmp_path, doubles, monkeypatch):
    """Probing a resident device claims the serial port or GPIO line its
    collector already holds. That does not report a fault, it creates one:
    the collector loses the resource, exits and restarts. It happened, and
    read as two failures on a node where nothing was wrong."""
    node = node_with(tmp_path, "  - {name: mppt, driver: good, mode: resident}\n")
    device = node.find("mppt")
    monkeypatch.setattr(runner, "collector_holding", lambda d: 4242)

    out = io.StringIO()
    failures = runner.check(node, out)
    assert failures == 0
    assert "ok" in out.getvalue() and "4242" in out.getvalue()


def test_a_resident_device_with_no_collector_is_still_probed(tmp_path, doubles, monkeypatch):
    """The whole point of leaving it probing: a resident device that nothing
    is running IS a fault, and is the one this must still catch."""
    node = node_with(tmp_path, "  - {name: mppt, driver: bad, mode: resident}\n")
    monkeypatch.setattr(runner, "collector_holding", lambda d: None)

    out = io.StringIO()
    assert runner.check(node, out) == 1
    assert "FAIL" in out.getvalue()


def test_only_resident_devices_are_treated_as_owned(tmp_path, doubles):
    """A poll device is read by a process that exits, so nothing holds it and
    probing is always safe. Skipping the probe for one would hide a fault."""
    node = node_with(tmp_path, "  - {name: a, driver: good, mode: poll}\n")
    assert runner.collector_holding(node.find("a")) is None


def test_the_collector_search_ignores_an_unrelated_process(tmp_path, doubles):
    """Matching too loosely would silence a genuinely dead device because
    some other command line happened to contain its name."""
    node = node_with(tmp_path, "  - {name: mppt, driver: good, mode: resident}\n")
    # Nothing on this machine is running a collector for a device called mppt
    # under test, so the search must come back empty rather than matching the
    # test runner's own command line.
    assert runner.collector_holding(node.find("mppt")) is None


# -- handing readings to load control ---------------------------------------

def controlling_node(tmp_path, body, control="""
control:
  enabled: false
  loads:
    cameras: {type: dc_branch, control_pin: 23}
"""):
    path = tmp_path / "node.yaml"
    path.write_text("site: s\nnode: n\nmeasurement: m\ndevices:\n" + body
                    + control)
    return config.load(path, set(REGISTRY))


@pytest.fixture
def cache(tmp_path, monkeypatch):
    """Point the reading cache somewhere a test can read it."""
    directory = tmp_path / "readings"
    monkeypatch.setattr(control_files, "READINGS_DIR", str(directory))
    return directory


def cached(directory):
    if not directory.is_dir():
        return {}
    return control_files.read_readings(
        str(directory), now=0.0, this_boot=control_files.boot_id())


def test_a_good_reading_is_handed_to_load_control(tmp_path, doubles, cache):
    node = controlling_node(tmp_path, "  - {name: a, driver: good, mode: poll}\n")
    runner.poll(node, NOW, io.StringIO(), io.StringIO())
    assert "a" in cached(cache)


def test_a_failed_read_does_not_refresh_the_cache(tmp_path, doubles, cache):
    """The dangerous direction, and the one that makes staleness meaningless.

    Refreshing the stamp without refreshing the value would leave a dead
    sensor looking current forever, and the ladder would go on trusting a
    number from before the device died. The old record is left to age out,
    which is what `stale_after` is measuring.
    """
    node = controlling_node(
        tmp_path, "  - {name: a, driver: good, mode: poll}\n")
    runner.poll(node, NOW, io.StringIO(), io.StringIO())
    record = cache / "a.json"
    before = record.read_text()

    broken = controlling_node(
        tmp_path, "  - {name: a, driver: bad, mode: poll}\n")
    runner.poll(broken, NOW, io.StringIO(), io.StringIO())
    assert record.read_text() == before, (
        "a failed read rewrote the cache record")


def test_a_disabled_device_does_not_reach_the_cache(tmp_path, doubles, cache):
    node = controlling_node(
        tmp_path, "  - {name: a, driver: good, mode: poll, enabled: false}\n")
    runner.poll(node, NOW, io.StringIO(), io.StringIO())
    assert cached(cache) == {}


def test_a_node_that_controls_nothing_writes_no_cache(tmp_path, doubles, cache):
    """A collector on a machine with no loads should not be creating runtime
    directories for a process that will never run there."""
    node = node_with(tmp_path, "  - {name: a, driver: good, mode: poll}\n")
    runner.poll(node, NOW, io.StringIO(), io.StringIO())
    assert cached(cache) == {}


def test_a_cache_that_cannot_be_written_does_not_cost_the_reading(
        tmp_path, doubles, monkeypatch):
    """The point reaching the database is the job. The cache is a convenience
    for a process on the same machine and it is published again in seconds."""
    monkeypatch.setattr(control_files, "READINGS_DIR", "/proc/nowhere/at/all")
    node = controlling_node(tmp_path, "  - {name: a, driver: good, mode: poll}\n")
    out = io.StringIO()
    written = runner.poll(node, NOW, out, io.StringIO())
    assert written == 1
    assert "value=2" in out.getvalue()


def test_the_cache_holds_the_same_numbers_the_database_gets(tmp_path, doubles,
                                                            cache):
    """A node file's thresholds are chosen by somebody reading a dashboard,
    so a controller acting on uncalibrated values would be comparing against
    bounds picked for corrected ones.

    calibration.py exists so there is one answer to where a number got
    changed, and a second path that skipped it would make that false.
    """
    node = controlling_node(
        tmp_path,
        "  - {name: a, driver: good, mode: poll, "
        "calibration: {value: {offset: 0.5}}}\n")
    out = io.StringIO()
    runner.poll(node, NOW, out, io.StringIO())
    assert "value=2.5" in out.getvalue(), "the database should see 2.5"
    assert cached(cache)["a"]["fields"]["value"] == 2.5, (
        "the controller saw the uncalibrated value")


def test_the_cache_is_calibrated_once_not_twice(tmp_path, doubles, cache):
    """Scale and offset are not idempotent. Applying them to an already
    corrected dict would square the scale."""
    node = controlling_node(
        tmp_path,
        "  - {name: a, driver: good, mode: poll, "
        "calibration: {value: {scale: 3}}}\n")
    runner.poll(node, NOW, io.StringIO(), io.StringIO())
    assert cached(cache)["a"]["fields"]["value"] == 6.0


def test_a_cache_failure_is_said_once_rather_than_never(tmp_path, doubles,
                                                        monkeypatch, capsys):
    """A cache that has never worked and one that is working look identical
    from outside. The usual cause is ownership: a hand run as root creates
    the directory and the service can then never write it.

    Once per process rather than once per device per interval, which would
    bury the readings it is printed beside. It goes to the process's own
    error stream rather than the collection's, because it is a fact about
    the machine rather than about this reading.
    """
    monkeypatch.setattr(runner, "_CACHE_COMPLAINED", False)
    monkeypatch.setattr(control_files, "READINGS_DIR", "/proc/nowhere/at/all")
    node = controlling_node(
        tmp_path, "  - {name: a, driver: good, mode: poll}\n"
                  "  - {name: b, driver: good, mode: poll}\n")
    runner.poll(node, NOW, io.StringIO(), io.StringIO())
    assert capsys.readouterr().err.count(
        "cannot write the reading cache") == 1


def test_the_streaming_path_also_feeds_the_cache(tmp_path, stream_doubles,
                                                 cache):
    """The charge controller downstairs is the resident device, so this is
    the path that will carry a real battery voltage to the controller."""
    node = controlling_node(
        tmp_path, "  - {name: a, driver: streamer, mode: resident}\n")
    runner.stream_device(node, node.find("a"), io.StringIO(), io.StringIO(),
                         max_readings=2, clock=frozen_clock)
    assert "a" in cached(cache)


def test_a_streamed_point_goes_out_before_the_cache_is_written(
        tmp_path, stream_doubles, cache):
    """The point reaching the database is the job. A slow cache write must
    not sit between the reading and the line that carries it."""
    order = []

    class Watching(io.StringIO):
        def flush(self):
            order.append("point")

    node = controlling_node(
        tmp_path, "  - {name: a, driver: streamer, mode: resident}\n")
    real = control_files.publish_reading

    def watched(*args, **kwargs):
        order.append("cache")
        return real(*args, **kwargs)

    import unittest.mock
    with unittest.mock.patch.object(control_files, "publish_reading", watched):
        runner.stream_device(node, node.find("a"), Watching(), io.StringIO(),
                             max_readings=1, clock=frozen_clock)
    assert order[:2] == ["point", "cache"], order
