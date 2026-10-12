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


# -- load and memory -------------------------------------------------------

def test_load_averages_come_from_the_kernel_not_a_computed_percentage():
    """A collector that runs once cannot measure a processor percentage.

    That needs two samples separated by time. The kernel already keeps the
    averages, so one read is honest where a computed figure would be a guess
    presented as a measurement.
    """
    fields = driver().read()
    assert fields["load_1m"] == pytest.approx(0.84)
    assert fields["load_5m"] == pytest.approx(1.42)
    assert fields["load_15m"] == pytest.approx(1.631)


def test_load_is_also_reported_per_processor():
    """Saturation means one, whatever size the machine is.

    Raw load cannot be judged without knowing the core count, and a
    dashboard cannot hold one threshold for a single core machine and
    another for a four core one.
    """
    fields = driver().read()
    assert fields["cpu_count"] == 4.0
    assert fields["load_per_core"] == pytest.approx(0.84 / 4)


def test_memory_is_reported_as_available_rather_than_free():
    """Free memory on a machine with a page cache means almost nothing.

    The fixture describes the shape of this project's one real memory
    failure: free sat near a hundred megabytes while more than twice that
    was actually obtainable. Reporting free would have raised an alarm every
    quiet night and taught everyone to ignore it.
    """
    fields = driver().read()
    assert fields["mem_available_mb"] == pytest.approx(229.7, abs=0.1)
    assert fields["mem_used_pct"] == pytest.approx(45.1, abs=0.1)
    # Free memory is not published at all, so nobody can alert on it.
    assert "mem_free_mb" not in fields


def test_swap_in_use_is_reported_because_it_is_the_early_warning():
    """On a machine booting from a memory card, swapping causes the slowness
    it also signals."""
    fields = driver().read()
    assert fields["swap_used_pct"] == pytest.approx(72.0, abs=0.1)


def test_a_kernel_without_available_memory_publishes_none_rather_than_free(tmp_path):
    """A different quantity under the same name is worse than no reading."""
    proc = tmp_path / "proc"
    proc.mkdir()
    (proc / "meminfo").write_text(
        "MemTotal:         428432 kB\nMemFree:          109876 kB\n")
    fields = HostDriver("host", {"sysfs_root": str(tmp_path),
                                 "report_address": False}, {}).read()
    assert fields["mem_available_mb"] is None
    assert fields["mem_used_pct"] is None


def test_load_and_memory_are_absent_rather_than_zero_when_unreadable(tmp_path):
    (tmp_path / "proc").mkdir()
    (tmp_path / "proc" / "uptime").write_text("1.0 1.0\n")
    fields = HostDriver("host", {"sysfs_root": str(tmp_path),
                                 "report_address": False}, {}).read()
    for name in ("load_1m", "load_per_core", "cpu_count",
                 "mem_available_mb", "swap_used_pct"):
        assert fields[name] is None, name


def test_a_check_reports_load_and_memory_alongside_temperature():
    message = driver().check()
    assert "load" in message and "memory" in message
