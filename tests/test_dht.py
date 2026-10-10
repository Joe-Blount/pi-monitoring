import os
import pathlib

import pytest

from monitoring.drivers.base import DriverError
from monitoring.drivers.dht import DhtDriver

IIO = str(pathlib.Path(__file__).resolve().parent / "fixtures" / "iio")


def driver(**params):
    params.setdefault("iio_root", IIO)
    params.setdefault("retries", 0)
    return DhtDriver("box", params, {})


def test_a_reading_is_converted_to_fahrenheit_and_percent():
    """The kernel reports thousandths. One unit system, converted here."""
    # 26125 is 26.125 C, which is 79.0 F. 50100 is 50.1 percent.
    assert driver().read() == {"temp": 79.0, "humidity": 50.1}


def test_the_sensor_is_found_by_name_not_by_being_first(tmp_path):
    """An industrial I/O bus carries unrelated devices. Picking the first
    would read a pressure sensor and call it humidity."""
    first = tmp_path / "iio:device0"
    first.mkdir()
    (first / "name").write_text("bmp280")

    second = tmp_path / "iio:device1"
    second.mkdir()
    (second / "name").write_text("dht11")
    (second / "in_temp_input").write_text("21000")
    (second / "in_humidityrelative_input").write_text("44000")

    values = DhtDriver("box", {"iio_root": str(tmp_path), "retries": 0},
                       {}).read()
    assert values["humidity"] == 44.0


def test_a_device_can_be_named_explicitly_when_several_are_present(tmp_path):
    for index, name in enumerate(("dht11", "dht11")):
        d = tmp_path / ("iio:device%d" % index)
        d.mkdir()
        (d / "name").write_text(name)
        (d / "in_temp_input").write_text("20000")
        (d / "in_humidityrelative_input").write_text("%d000" % (40 + index))
    chosen = DhtDriver("box", {"iio_root": str(tmp_path),
                               "iio_device": "iio:device1", "retries": 0}, {})
    assert chosen.read()["humidity"] == 41.0


def test_two_sensors_and_no_choice_is_refused_rather_than_guessed(tmp_path):
    for index in (0, 1):
        d = tmp_path / ("iio:device%d" % index)
        d.mkdir()
        (d / "name").write_text("dht11")
    with pytest.raises(DriverError) as exc:
        DhtDriver("box", {"iio_root": str(tmp_path), "retries": 0}, {}).read()
    assert "which one" in str(exc.value)


def test_a_missing_sensor_gives_the_line_to_add_and_says_to_reboot(tmp_path):
    """The overlay is named dht11 whatever sensor is attached, which is the
    commonest reason people conclude a DHT22 is unsupported."""
    with pytest.raises(DriverError) as exc:
        DhtDriver("box", {"iio_root": str(tmp_path), "pin": 17, "retries": 0}, {}).read()
    message = str(exc.value)
    assert "dtoverlay=dht11,gpiopin=17" in message
    assert "reboot" in message
    assert "DHT22 included" in message


def test_a_failed_read_is_reported_rather_than_published(tmp_path):
    """The kernel reports a failed checksum as an ordinary read error, and
    these sensors fail often enough that it must never become a value."""
    device = tmp_path / "iio:device0"
    device.mkdir()
    (device / "name").write_text("dht11")
    (device / "in_temp_input").write_text("20000")
    # A directory where a file should be: reading it raises, as a failed
    # conversion does on real hardware.
    (device / "in_humidityrelative_input").mkdir()
    with pytest.raises(DriverError):
        DhtDriver("box", {"iio_root": str(tmp_path), "retries": 0}, {}).read()


@pytest.mark.parametrize("humidity,why", [
    ("150000", "above any possible humidity"),
    ("-1000", "below zero"),
])
def test_an_implausible_humidity_is_refused(tmp_path, humidity, why):
    """The checksum cannot catch a value that is well formed and plainly
    wrong, so plausibility is checked here."""
    device = tmp_path / "iio:device0"
    device.mkdir()
    (device / "name").write_text("dht11")
    (device / "in_temp_input").write_text("20000")
    (device / "in_humidityrelative_input").write_text(humidity)
    with pytest.raises(DriverError) as exc:
        DhtDriver("box", {"iio_root": str(tmp_path), "retries": 0}, {}).read()
    assert "not plausible" in str(exc.value)


def test_an_implausible_temperature_is_refused(tmp_path):
    device = tmp_path / "iio:device0"
    device.mkdir()
    (device / "name").write_text("dht11")
    (device / "in_temp_input").write_text("200000")      # 200 C
    (device / "in_humidityrelative_input").write_text("50000")
    with pytest.raises(DriverError) as exc:
        DhtDriver("box", {"iio_root": str(tmp_path), "retries": 0}, {}).read()
    assert "not plausible" in str(exc.value)


def test_the_plausible_range_is_configurable(tmp_path):
    """Using a bound the default would also accept proves nothing: the
    parameter could be ignored entirely and the test would pass."""
    device = tmp_path / "iio:device0"
    device.mkdir()
    (device / "name").write_text("dht11")
    (device / "in_temp_input").write_text("-30000")      # -30 C, -22 F
    (device / "in_humidityrelative_input").write_text("50000")

    # The default floor is -40 F, so -22 F is accepted.
    assert DhtDriver("box", {"iio_root": str(tmp_path), "retries": 0},
                     {}).read()["temp"] == -22.0

    # Raise the floor above it and the same reading must now be refused.
    with pytest.raises(DriverError) as exc:
        DhtDriver("box", {"iio_root": str(tmp_path), "retries": 0,
                          "min_temp_f": 0.0}, {}).read()
    assert "not plausible" in str(exc.value)


def test_retries_are_attempted_and_the_waits_are_spaced(tmp_path, monkeypatch):
    """A single failure is routine. Retrying immediately would return the same
    stale answer, so attempts are spaced by the sensor's own interval."""
    device = tmp_path / "iio:device0"
    device.mkdir()
    (device / "name").write_text("dht11")
    (device / "in_temp_input").write_text("20000")
    (device / "in_humidityrelative_input").write_text("150000")   # implausible

    slept = []
    monkeypatch.setattr("monitoring.drivers.dht.time.sleep", slept.append)
    with pytest.raises(DriverError) as exc:
        DhtDriver("box", {"iio_root": str(tmp_path), "retries": 3}, {}).read()
    assert slept == [2.0, 2.0, 2.0]
    assert "4 attempts" in str(exc.value)


def test_a_later_attempt_can_succeed(tmp_path, monkeypatch):
    device = tmp_path / "iio:device0"
    device.mkdir()
    (device / "name").write_text("dht11")
    (device / "in_temp_input").write_text("20000")
    humidity = device / "in_humidityrelative_input"
    humidity.write_text("150000")

    def fix_it_then(_seconds):
        humidity.write_text("45000")

    monkeypatch.setattr("monitoring.drivers.dht.time.sleep", fix_it_then)
    assert DhtDriver("box", {"iio_root": str(tmp_path), "retries": 3},
                     {}).read()["humidity"] == 45.0


def test_the_failure_message_points_at_wiring(tmp_path, monkeypatch):
    device = tmp_path / "iio:device0"
    device.mkdir()
    (device / "name").write_text("dht11")
    (device / "in_temp_input").write_text("20000")
    (device / "in_humidityrelative_input").write_text("150000")
    monkeypatch.setattr("monitoring.drivers.dht.time.sleep", lambda _s: None)
    with pytest.raises(DriverError) as exc:
        DhtDriver("box", {"iio_root": str(tmp_path), "retries": 1}, {}).read()
    message = str(exc.value)
    assert "pull-up" in message and "gpiopin" in message


def test_check_reports_presence_even_when_a_reading_fails(tmp_path):
    """Being present matters more than one reading succeeding, because a
    single failure is routine for this sensor."""
    device = tmp_path / "iio:device0"
    device.mkdir()
    (device / "name").write_text("dht11")
    (device / "in_temp_input").write_text("20000")
    (device / "in_humidityrelative_input").write_text("150000")
    detail = DhtDriver("box", {"iio_root": str(tmp_path), "retries": 0}, {}).check()
    assert "present" in detail and "not unusual" in detail


def test_check_reports_the_values_when_it_works():
    assert "79.0 F" in driver().check()




# --- the name the kernel actually reports ----------------------------------

def _iio(tmp_path, devices):
    """Build an industrial I/O tree: {device number: reported name}."""
    root = tmp_path / "iio"
    for number, reported in devices.items():
        d = root / ("iio:device%d" % number)
        d.mkdir(parents=True)
        (d / "name").write_text(reported + "\n")
    return str(root)


def test_a_sensor_is_found_under_the_name_the_kernel_really_uses(tmp_path):
    """The kernel names the device after its device tree node, including a
    unit address: GPIO17 reports dht11@11, not dht11. An exact comparison
    finds nothing on a real machine, which is a mistake only hardware shows."""
    root = _iio(tmp_path, {0: "dht11@11"})
    driver = DhtDriver("box", {"iio_root": root, "pin": 17}, {})
    assert driver._device_path().endswith("iio:device0")


def test_a_sensor_with_no_unit_address_is_still_found(tmp_path):
    """Older kernels report the bare name. Both must work."""
    root = _iio(tmp_path, {0: "dht11"})
    driver = DhtDriver("box", {"iio_root": root, "pin": 17}, {})
    assert driver._device_path().endswith("iio:device0")


def test_two_sensors_are_told_apart_by_the_pin_in_their_name(tmp_path):
    """The unit address is the GPIO in hexadecimal, so it is the only thing
    that distinguishes two of these. Refusing to choose would be worse: both
    would be unreadable rather than one."""
    root = _iio(tmp_path, {0: "dht11@11", 1: "dht11@18"})     # GPIO 17 and 24
    assert DhtDriver("a", {"iio_root": root, "pin": 17}, {})._device_path() \
        .endswith("iio:device0")
    assert DhtDriver("b", {"iio_root": root, "pin": 24}, {})._device_path() \
        .endswith("iio:device1")


def test_a_sensor_on_a_different_pin_is_not_offered(tmp_path):
    """Reading the wrong sensor silently is worse than reporting none."""
    root = _iio(tmp_path, {0: "dht11@11"})
    driver = DhtDriver("box", {"iio_root": root, "pin": 24}, {})
    with pytest.raises(DriverError) as raised:
        driver._device_path()
    assert "no DHT sensor" in str(raised.value)


def test_another_kind_of_iio_device_is_ignored(tmp_path):
    """A pressure sensor on the same bus must not be read as a thermometer."""
    root = _iio(tmp_path, {0: "bmp280", 1: "dht11@11"})
    driver = DhtDriver("box", {"iio_root": root, "pin": 17}, {})
    assert driver._device_path().endswith("iio:device1")


def test_the_unit_address_is_read_as_hexadecimal():
    """GPIO 17 appears as @11, not @17. Reading it as decimal would match the
    wrong sensor on a machine with two."""
    assert DhtDriver._reported_pin("dht11@11") == 17
    assert DhtDriver._reported_pin("dht11@4") == 4
    assert DhtDriver._reported_pin("dht11") is None
