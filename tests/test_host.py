import pathlib

import pytest

from monitoring.drivers.base import DriverError
from monitoring.drivers.host import HostDriver

FIXTURE = str(pathlib.Path(__file__).resolve().parent / "fixtures" / "host")


def driver(**params):
    params.setdefault("sysfs_root", FIXTURE)
    params.setdefault("report_address", False)
    return HostDriver("host", params, {})


def test_readings_come_from_the_fixture_tree():
    """Injectable paths are what make this testable away from a Pi."""
    fields = driver().read()
    assert fields["uptime_seconds"] == 123456.8
    assert fields["boot_time"] == 1700000000.0


def test_temperature_is_converted_to_fahrenheit():
    """One unit system, converted in the driver, never in the dashboard."""
    # 47300 millidegrees C is 47.3 C, which is 117.1 F.
    assert driver().read()["cpu_temp_f"] == 117.1


def test_disk_usage_is_reported():
    fields = driver(filesystem="/").read()
    assert fields["root_free_gb"] > 0
    assert 0 <= fields["root_used_pct"] <= 100


def test_address_is_omitted_when_not_wanted():
    assert "ip" not in driver().read()


def test_missing_files_give_none_rather_than_raising(tmp_path):
    """One unreadable value must not cost the readings beside it."""
    fields = HostDriver("host", {"sysfs_root": str(tmp_path),
                                 "report_address": False}, {}).read()
    assert fields["cpu_temp_f"] is None
    assert fields["uptime_seconds"] is None
    assert fields["root_free_gb"] is not None   # the filesystem still answers


def test_check_passes_against_the_fixture_tree():
    assert "uptime readable" in driver().check()


def test_check_fails_clearly_when_nothing_is_there(tmp_path):
    with pytest.raises(DriverError) as exc:
        HostDriver("host", {"sysfs_root": str(tmp_path)}, {}).check()
    assert "cannot read" in str(exc.value)
