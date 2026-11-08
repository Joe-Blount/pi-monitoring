"""The override module: the one writer of the switches file.

The command line and the override page both change the switches file through
this module, so the two cannot disagree about what a force looks like.
"""

import datetime
import json
import threading
import zoneinfo

import pytest

from monitoring.control import files, override

ZONE = zoneinfo.ZoneInfo("America/Chicago")


def read(path):
    with open(path) as handle:
        return json.load(handle)


def test_a_force_is_written_with_its_state_and_expiry(tmp_path):
    path = str(tmp_path / "switches.json")
    override.force("cameras", "on", 1000.0, path=path)
    assert read(path)["loads"]["cameras"] == {
        "force": "on", "expires_epoch": 1000.0}


def test_a_force_keeps_the_other_loads_and_the_shedding_switch(tmp_path):
    path = str(tmp_path / "switches.json")
    files.write_switches({"shedding_enabled": False,
                          "loads": {"pump": {"force": "off",
                                             "expires_epoch": 5.0}}}, path)
    override.force("cameras", "on", 1000.0, path=path)
    record = read(path)
    assert record["shedding_enabled"] is False
    assert record["loads"]["pump"] == {"force": "off", "expires_epoch": 5.0}


def test_clear_removes_only_the_named_force(tmp_path):
    path = str(tmp_path / "switches.json")
    override.force("cameras", "on", 1000.0, path=path)
    override.force("pump", "off", 1000.0, path=path)
    override.clear("cameras", path=path)
    assert set(read(path)["loads"]) == {"pump"}


def test_set_shedding_writes_the_switch(tmp_path):
    path = str(tmp_path / "switches.json")
    override.set_shedding(False, path=path)
    assert read(path)["shedding_enabled"] is False
    override.set_shedding(True, path=path)
    assert read(path)["shedding_enabled"] is True


def test_a_state_other_than_on_or_off_is_refused(tmp_path):
    with pytest.raises(override.OverrideError):
        override.force("cameras", "sideways", 1000.0,
                       path=str(tmp_path / "s.json"))


def test_a_load_outside_the_known_set_is_refused(tmp_path):
    path = tmp_path / "switches.json"
    with pytest.raises(override.OverrideError, match="camras"):
        override.force("camras", "on", 1000.0, path=str(path),
                       known={"cameras"})
    assert not path.exists()


def test_a_force_without_a_finite_expiry_is_refused(tmp_path):
    for expires in (None, float("inf"), float("nan"), True):
        with pytest.raises(override.OverrideError):
            override.force("cameras", "on", expires,
                           path=str(tmp_path / "s.json"))


def test_expiry_for_a_duration_counts_from_now():
    assert override.expiry_for("15m", now=100.0) == 1000.0


def test_expiry_for_a_bad_duration_is_refused():
    with pytest.raises(override.OverrideError):
        override.expiry_for("soon", now=0.0)


def test_expiry_until_reads_the_time_in_the_sites_zone():
    expected = datetime.datetime(2026, 11, 8, 18, 30, tzinfo=ZONE).timestamp()
    assert override.expiry_until("2026-11-08 18:30", ZONE) == expected


def test_expiry_until_accepts_the_form_a_browser_sends():
    expected = datetime.datetime(2026, 11, 8, 18, 30, tzinfo=ZONE).timestamp()
    assert override.expiry_until("2026-11-08T18:30", ZONE) == expected


def test_expiry_until_refuses_an_unreadable_time():
    with pytest.raises(override.OverrideError):
        override.expiry_until("tomorrow", ZONE)


def test_two_writers_at_once_keep_both_changes(tmp_path):
    path = str(tmp_path / "switches.json")
    names = ["load%d" % index for index in range(20)]
    threads = [threading.Thread(target=override.force,
                                args=(name, "on", 1000.0),
                                kwargs={"path": path})
               for name in names]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert set(read(path)["loads"]) == set(names)


def test_the_files_stay_readable_by_others_under_a_strict_umask(tmp_path):
    import os
    import stat
    path = str(tmp_path / "switches.json")
    old = os.umask(0o077)
    try:
        override.force("cameras", "on", 1000.0, path=path)
    finally:
        os.umask(old)
    for name in (path, path + ".lock"):
        assert stat.S_IMODE(os.stat(name).st_mode) & 0o044 == 0o044


def test_the_lock_is_never_taken_through_a_link(tmp_path):
    import os
    target = tmp_path / "elsewhere"
    target.write_text("")
    os.chmod(target, 0o600)
    path = str(tmp_path / "switches.json")
    os.symlink(target, path + ".lock")
    with pytest.raises(OSError):
        override.force("cameras", "on", 1000.0, path=path)
    assert oct(os.stat(target).st_mode & 0o777) == oct(0o600)
