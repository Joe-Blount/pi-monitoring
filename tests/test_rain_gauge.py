import json

import pytest

from monitoring.drivers.base import DriverError
from monitoring.drivers.rain_gauge import RainCounter, RainGaugeDriver


class Clock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now


def counter(tmp_path, **kwargs):
    kwargs.setdefault("state_file", str(tmp_path / "rain.json"))
    kwargs.setdefault("inches_per_tip", 0.011)
    return RainCounter(**kwargs)


def test_each_tip_is_a_fixed_amount_of_rain(tmp_path):
    c = counter(tmp_path)
    for _ in range(3):
        c.tip()
    assert c.tips == 3
    assert c.inches == 0.033


def test_the_count_survives_a_restart(tmp_path):
    """Held only in memory the total resets on every power event, and the
    graph then shows rain that stopped rather than a Pi that restarted."""
    first = counter(tmp_path)
    first.tip()
    first.tip()

    second = counter(tmp_path)
    assert second.tips == 2
    assert second.inches == 0.022


def test_the_state_file_is_replaced_atomically(tmp_path, monkeypatch):
    """The claim is that a failure partway through leaves the previous count
    intact rather than something unparsable. Checking the content after a
    successful save does not show that; a plain overwrite passes it too."""
    c = counter(tmp_path)
    c.tip()
    assert json.loads((tmp_path / "rain.json").read_text())["tips"] == 1
    assert [p.name for p in tmp_path.iterdir()] == ["rain.json"]

    def fail_midway(src, dst):
        raise OSError("interrupted")

    monkeypatch.setattr("os.replace", fail_midway)
    c.tip()

    surviving = json.loads((tmp_path / "rain.json").read_text())
    assert surviving["tips"] == 1, "the previous count must survive a failure"
    assert [p.name for p in tmp_path.iterdir()] == ["rain.json"]


def test_a_corrupt_state_file_starts_from_zero_rather_than_refusing(tmp_path):
    """A lost total is a gap in one series. Refusing to start loses every
    reading from then on as well."""
    (tmp_path / "rain.json").write_text("{not json at all")
    c = counter(tmp_path)
    assert c.tips == 0
    c.tip()
    assert c.tips == 1


@pytest.mark.parametrize("bad_path", [
    "nodir/deeper/x/\0bad",          # invalid: raises ValueError, not OSError
    "/proc/cannot/write/here.json",  # unwritable location
])
def test_a_state_file_that_cannot_be_written_does_not_stop_the_counting(
        tmp_path, monkeypatch, bad_path):
    # Run from a temporary directory: a relative bad path would otherwise
    # create directories in the repository, which an earlier version did.
    monkeypatch.chdir(tmp_path)
    """Persistence is best effort; counting is not. A surprise from the
    filesystem must not be the thing that stops rain being measured."""
    c = RainCounter(bad_path, 0.011)
    c.tip()
    c.tip()
    assert c.tips == 2
    assert c.inches == 0.022


def test_a_failed_save_leaves_no_temporary_file_behind(tmp_path, monkeypatch):
    c = RainCounter(str(tmp_path / "rain.json"), 0.011)

    def explode(src, dst):
        raise OSError("no")

    monkeypatch.setattr("os.replace", explode)
    c.tip()
    assert c.tips == 1
    assert list(tmp_path.iterdir()) == []


def test_no_state_file_means_memory_only(tmp_path):
    c = RainCounter(None, 0.011)
    c.tip()
    assert c.tips == 1


def test_time_since_the_last_tip_is_published(tmp_path):
    clock = Clock(1000.0)
    c = counter(tmp_path, clock=clock)
    c.tip()
    clock.now = 1090.0
    assert c.fields()["seconds_since_tip"] == 90.0


def test_before_any_tip_there_is_no_time_since(tmp_path):
    assert "seconds_since_tip" not in counter(tmp_path).fields()




def test_the_bucket_size_is_configurable(tmp_path):
    c = counter(tmp_path, inches_per_tip=0.01)
    c.tip()
    assert c.inches == 0.01


def test_the_driver_requires_a_pin():
    with pytest.raises(DriverError) as exc:
        RainGaugeDriver("rain", {}, {})
    assert "pin" in str(exc.value)


def test_the_driver_reads_the_carried_over_total_without_gpio(tmp_path):
    """read() must not need the pin, so a total is available even where the
    GPIO library is missing."""
    state = tmp_path / "rain.json"
    state.write_text(json.dumps({"tips": 5, "last_tip": None}))
    driver = RainGaugeDriver("rain", {"pin": 6, "state_file": str(state),
                                      "inches_per_tip": 0.011}, {})
    assert driver.read()["rain_tips"] == 5.0
    assert driver.read()["rain_inches"] == 0.055


def test_attaching_without_gpiozero_says_what_to_install(tmp_path, monkeypatch):
    import builtins
    real_import = builtins.__import__

    def no_gpiozero(name, *args, **kwargs):
        if name == "gpiozero":
            raise ImportError("no module named gpiozero")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_gpiozero)
    driver = RainGaugeDriver("rain", {"pin": 6}, {})
    with pytest.raises(DriverError) as exc:
        driver.check()
    assert "python3-gpiozero" in str(exc.value)


def test_a_state_file_holding_the_wrong_shape_does_not_crash_the_child(tmp_path, capsys):
    """A JSON list where an object was expected used to raise, and the resident
    child then restarted every ten seconds forever."""
    state = tmp_path / "rain.json"
    state.write_text('["not", "an", "object"]')
    c = RainCounter(str(state), 0.011)
    assert c.tips == 0
    c.tip()
    assert c.tips == 1
    assert "could not read" in capsys.readouterr().err


def test_an_unreadable_state_file_says_so_rather_than_resetting_in_silence(
        tmp_path, capsys):
    """A total that quietly restarts at zero looks like rain that stopped."""
    state = tmp_path / "rain.json"
    state.write_text("{not json")
    RainCounter(str(state), 0.011)
    assert "starts again from zero" in capsys.readouterr().err


def test_the_state_file_is_readable_by_more_than_its_creator(tmp_path):
    """Written once by root during bring-up, a 0600 file becomes unreadable to
    the collector, which then restarts the total at zero on every boot."""
    import os
    import stat
    state = tmp_path / "rain.json"
    c = RainCounter(str(state), 0.011)
    c.tip()
    mode = stat.S_IMODE(os.stat(state).st_mode)
    assert mode & stat.S_IRGRP and mode & stat.S_IROTH, oct(mode)


# -- the loop that actually runs in production -------------------------------

@pytest.fixture
def mock_pins():
    """gpiozero's own mock factory, so the real driver can be exercised."""
    gpiozero = pytest.importorskip("gpiozero")
    from gpiozero.pins.mock import MockFactory
    previous = gpiozero.Device.pin_factory
    gpiozero.Device.pin_factory = MockFactory()
    yield gpiozero.Device.pin_factory
    gpiozero.Device.pin_factory.reset()
    gpiozero.Device.pin_factory = previous


def tip_the_bucket(factory, pin=6):
    """Close and release the reed switch, which pulls the pin to ground."""
    p = factory.pin(pin)
    p.drive_low()
    p.drive_high()


def test_only_a_falling_edge_counts(mock_pins, tmp_path):
    """The switch closes to ground against a pull-up, so a tip is the pin
    going LOW. Driving low and high together hides the polarity entirely:
    with the pull inverted the pin never falls, the gauge counts nothing
    forever, and the dashboard shows a healthy flat line."""
    driver = RainGaugeDriver("rain", {"pin": 6, "bounce_ms": 0,
                                      "state_file": str(tmp_path / "r.json")}, {})
    try:
        driver.check()
        pin = mock_pins.pin(6)

        pin.drive_low()
        assert driver.counter.tips == 1, "a falling edge is a tip"

        pin.drive_high()
        assert driver.counter.tips == 1, "the bucket righting itself is not"
    finally:
        driver.close()


def test_a_real_tip_on_the_pin_is_counted(mock_pins, tmp_path):
    """Everything above this tests the counter. This tests the wiring to it:
    a falling edge on the pin must reach the count."""
    driver = RainGaugeDriver("rain", {"pin": 6, "bounce_ms": 0,
                                      "state_file": str(tmp_path / "r.json"),
                                      "inches_per_tip": 0.011}, {})
    try:
        driver.check()                      # attaches to the pin
        assert driver.counter.tips == 0
        tip_the_bucket(mock_pins)
        assert driver.counter.tips == 1
        assert driver.read()["rain_inches"] == 0.011
    finally:
        driver.close()


def test_the_stream_publishes_when_the_bucket_tips(mock_pins, tmp_path):
    """The production loop. It had no test at all, despite being the only code
    that runs for this device once installed.

    The elapsed time is asserted, not just the value: without being woken by
    the tip this still produces the right answer, a whole heartbeat later.
    """
    driver = RainGaugeDriver("rain", {"pin": 6, "bounce_ms": 0,
                                      "emit_every": "30s",
                                      "state_file": str(tmp_path / "r.json")}, {})
    try:
        stream = driver.stream()
        first = next(stream)                # the opening reading
        assert first["rain_tips"] == 0.0

        import time as _time
        tip_the_bucket(mock_pins)
        began = _time.time()
        second = next(stream)
        waited = _time.time() - began

        assert second["rain_tips"] == 1.0
        assert second["rain_inches"] == 0.011
        assert waited < 1.0, (
            "took %.1fs, so it waited for the heartbeat rather than being "
            "woken by the tip" % waited)
    finally:
        driver.close()


def test_the_stream_publishes_even_when_it_is_not_raining(mock_pins, tmp_path):
    """Without this the series simply stops in dry weather, which is
    indistinguishable from the gauge having failed."""
    driver = RainGaugeDriver("rain", {"pin": 6, "bounce_ms": 0,
                                      "emit_every": "0.2s",
                                      "state_file": str(tmp_path / "r.json")}, {})
    try:
        stream = driver.stream()
        next(stream)
        heartbeat = next(stream)            # no tip happened at all
        assert heartbeat["rain_tips"] == 0.0
    finally:
        driver.close()


def test_the_count_survives_being_reattached(mock_pins, tmp_path):
    """A restart must not lose the total, which is the whole point of the
    state file. Exercised through the driver rather than the counter."""
    state = str(tmp_path / "r.json")
    first = RainGaugeDriver("rain", {"pin": 6, "bounce_ms": 0,
                                     "state_file": state}, {})
    try:
        first.check()
        tip_the_bucket(mock_pins)
        tip_the_bucket(mock_pins)
    finally:
        first.close()

    second = RainGaugeDriver("rain", {"pin": 6, "bounce_ms": 0,
                                      "state_file": state}, {})
    try:
        assert second.read()["rain_tips"] == 2.0
    finally:
        second.close()
