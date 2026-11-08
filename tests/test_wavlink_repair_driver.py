import json

import pytest

from monitoring.drivers.base import DriverError
from monitoring.drivers.wavlink_repair import WavlinkRepairDriver

NOW = 1_800_000_000.0


def driver(tmp_path, record=None):
    path = tmp_path / "state.json"
    if record is not None:
        path.write_text(json.dumps(record))
    d = WavlinkRepairDriver("wavlink_repair", {"path": str(path)}, {})
    d._now = lambda: NOW
    return d


def fields(d):
    (point,) = d.read()
    return point.fields


def test_the_last_attempt_and_the_last_restore_are_published(tmp_path):
    d = driver(tmp_path, {"sightings": 1, "attempts": [
        {"at": NOW - 3 * 86400, "result": "restored"},
        {"at": NOW - 3600, "result": "failed: the unit refused both passwords"},
    ]})
    got = fields(d)
    assert got["last_attempt_at"] == NOW - 3600
    assert got["last_result"].startswith("failed")
    assert got["last_restored_at"] == NOW - 3 * 86400
    assert got["attempts_last_day"] == 1.0
    assert got["attempts_recorded"] == 2.0
    assert got["sightings"] == 1.0


def test_a_record_with_no_attempts_publishes_zero_and_no_times(tmp_path):
    got = fields(driver(tmp_path, {"sightings": 0, "attempts": []}))
    assert got["attempts_last_day"] == 0.0
    assert "last_attempt_at" not in got and "last_restored_at" not in got


def test_a_missing_record_is_a_failure_that_names_the_timer(tmp_path):
    with pytest.raises(DriverError) as raised:
        driver(tmp_path).read()
    assert "timer" in str(raised.value)
