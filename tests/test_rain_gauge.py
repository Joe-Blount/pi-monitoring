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


def test_the_state_file_is_replaced_atomically(tmp_path):
    """A power cut partway through writing would otherwise leave something
    unparsable, and surviving power cuts is the entire point of the file."""
    c = counter(tmp_path)
    c.tip()
    saved = json.loads((tmp_path / "rain.json").read_text())
    assert saved["tips"] == 1
    # No temporary files are left behind.
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


def test_published_fields_are_numbers_suited_to_line_protocol(tmp_path):
    from monitoring import lineproto
    c = counter(tmp_path)
    c.tip()
    line = lineproto.line("blind1", {"location": "outdoor"}, c.fields())
    assert "rain_inches=0.011" in line
    assert "rain_tips=1.0" in line


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
