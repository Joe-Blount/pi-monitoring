import pathlib

import pytest

from monitoring.drivers.base import DriverError
from monitoring.drivers.ds18b20 import Ds18b20Driver

BUS = str(pathlib.Path(__file__).resolve().parent / "fixtures" / "w1")
GOOD = "28-3ce1d44326bf"
BAD_CRC = "28-000000000001"


def driver(device_id=GOOD, **params):
    params.setdefault("bus_root", BUS)
    params["device_id"] = device_id
    return Ds18b20Driver("outdoor_temp", params, {})


def test_temperature_is_converted_to_fahrenheit():
    """One unit system, converted in the driver, never in the dashboard."""
    # 26125 thousandths of a degree C is 26.125 C, which is 79.0 F.
    assert driver().read() == {"temp": 79.0}


def test_the_field_name_can_be_chosen():
    assert "outdoor_temp" in driver(field="outdoor_temp").read()


def test_a_failed_sensor_checksum_is_an_error_not_a_reading():
    """The sensor checks its own bytes. Ignoring that gives plausible
    nonsense, which is worse than no reading."""
    with pytest.raises(DriverError) as exc:
        driver(BAD_CRC).read()
    assert "checksum" in str(exc.value)


def test_a_missing_sensor_says_what_is_actually_present(tmp_path):
    """The usual cause is a mistyped id, so listing the real ones turns a
    puzzle into an obvious fix."""
    with pytest.raises(DriverError) as exc:
        driver("28-doesnotexist").read()
    message = str(exc.value)
    assert "28-doesnotexist" in message and GOOD in message


def test_an_empty_bus_points_at_the_usual_causes(tmp_path):
    with pytest.raises(DriverError) as exc:
        Ds18b20Driver("t", {"bus_root": str(tmp_path), "device_id": GOOD}, {}).read()
    assert "dtoverlay=w1-gpio" in str(exc.value)
    assert "pull-up" in str(exc.value)


def test_exactly_85_c_is_rejected_as_the_reset_value(tmp_path):
    """85 C is what the register holds before the first conversion. Treating
    it as real has sent people chasing a heat problem that was a wiring
    problem."""
    sensor = tmp_path / "28-aaaaaaaaaaaa"
    sensor.mkdir()
    (sensor / "w1_slave").write_text(
        "a2 01 4b 46 7f ff 0c 10 d8 : crc=d8 YES\n"
        "a2 01 4b 46 7f ff 0c 10 d8 t=85000\n")
    with pytest.raises(DriverError) as exc:
        Ds18b20Driver("t", {"bus_root": str(tmp_path),
                            "device_id": "28-aaaaaaaaaaaa"}, {}).read()
    assert "85 C" in str(exc.value) and "power" in str(exc.value)


def test_the_reset_value_can_be_accepted_where_it_is_plausible(tmp_path):
    sensor = tmp_path / "28-aaaaaaaaaaaa"
    sensor.mkdir()
    (sensor / "w1_slave").write_text(
        "a2 01 4b 46 7f ff 0c 10 d8 : crc=d8 YES\n"
        "a2 01 4b 46 7f ff 0c 10 d8 t=85000\n")
    values = Ds18b20Driver("t", {"bus_root": str(tmp_path),
                                 "device_id": "28-aaaaaaaaaaaa",
                                 "reject_reset_value": False}, {}).read()
    assert values["temp"] == 185.0


def test_negative_temperatures_work(tmp_path):
    sensor = tmp_path / "28-bbbbbbbbbbbb"
    sensor.mkdir()
    (sensor / "w1_slave").write_text(
        "a2 01 4b 46 7f ff 0c 10 d8 : crc=d8 YES\n"
        "a2 01 4b 46 7f ff 0c 10 d8 t=-10500\n")
    values = Ds18b20Driver("t", {"bus_root": str(tmp_path),
                                 "device_id": "28-bbbbbbbbbbbb"}, {}).read()
    assert values["temp"] == 13.1          # -10.5 C


def test_a_truncated_file_is_an_error(tmp_path):
    sensor = tmp_path / "28-cccccccccccc"
    sensor.mkdir()
    (sensor / "w1_slave").write_text("a2 01 : crc=d8 YES\n")
    with pytest.raises(DriverError):
        Ds18b20Driver("t", {"bus_root": str(tmp_path),
                            "device_id": "28-cccccccccccc"}, {}).read()


def test_check_confirms_the_sensor_without_judging_the_value():
    assert GOOD in driver().check()


def test_a_device_id_is_required():
    with pytest.raises(DriverError):
        Ds18b20Driver("t", {"bus_root": BUS}, {})
