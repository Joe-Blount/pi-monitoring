"""The committed energy dashboard is what its builder makes from the query.

The dashboard's daily cells are the daily energy query with a different
start. A dashboard edited by hand, or a query changed without rebuilding,
would show figures the documented query does not compute.
"""

import json
import pathlib
import subprocess
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent
BUILD = REPO / "docs" / "dashboards" / "build.py"
DASHBOARD = REPO / "docs" / "dashboards" / "blind1-energy.json"
HEALTH = REPO / "docs" / "dashboards" / "pi-health.json"
CHINS = REPO / "docs" / "dashboards" / "blind1-chins.json"


def _template(path=DASHBOARD):
    return json.loads(path.read_text())


def test_the_committed_dashboard_matches_its_builder():
    result = subprocess.run([sys.executable, str(BUILD), "--check"],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def _charts(path=DASHBOARD):
    (dashboard,) = _template(path)
    return dashboard["spec"]["charts"]


@pytest.mark.parametrize("path", [DASHBOARD, HEALTH, CHINS])
def test_the_dashboard_is_a_template_in_the_format_the_cloud_export_writes(path):
    (dashboard,) = _template(path)
    assert dashboard["apiVersion"] == "influxdata.com/v2alpha1"
    assert dashboard["kind"] == "Dashboard"
    for chart in dashboard["spec"]["charts"]:
        assert chart["kind"] in {"Xy", "Single_Stat", "Table"}
        assert chart["queries"][0]["query"].strip()


def test_the_daily_cells_follow_the_dashboards_time_range():
    daily = [c for c in _charts() if c["name"].startswith("Daily energy")]
    assert len(daily) == 3
    for chart in daily:
        text = chart["queries"][0]["query"]
        assert "v.timeRangeStart" in text
        assert "lookback" not in text


@pytest.mark.parametrize("path", [DASHBOARD, HEALTH, CHINS])
def test_the_cells_tile_the_grid_without_overlapping(path):
    taken = set()
    for chart in _charts(path):
        x, y, w, h = chart["xPos"], chart["yPos"], chart["width"], chart["height"]
        assert 0 <= x and x + w <= 12
        for column in range(x, x + w):
            for row in range(y, y + h):
                assert (column, row) not in taken
                taken.add((column, row))


def test_the_health_dashboard_names_no_machine():
    """A machine added later must appear with no edit to the dashboard."""
    files = [p for p in (REPO / "sites").glob("*/*.yaml")
             if p.parent.name != "example"]
    names = {p.stem for p in files} | {p.parent.name for p in files}
    for chart in _charts(HEALTH):
        text = chart["queries"][0]["query"]
        for name in names:
            assert '"%s"' % name not in text, (chart["name"], name)


def test_the_health_dashboard_reads_only_fields_the_host_device_publishes():
    """A renamed host field would otherwise blank a column with no error."""
    import importlib.util
    from tests.test_node_files import known_driver_fields
    spec = importlib.util.spec_from_file_location("build", BUILD)
    build = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(build)
    published = set(known_driver_fields()["host"])
    derived = {"site", "node", "seen", "up"}
    columns = {field for field, _ in build.MACHINE_COLUMNS} - derived
    assert columns <= published
    graphed = {chart["queries"][0]["query"].split('r._field == "')[1].split('"')[0]
               for chart in _charts(HEALTH) if chart["kind"] == "Xy"}
    assert graphed <= published


def test_the_chins_dashboard_reads_only_fields_the_pack_publishes():
    """A renamed pack field would otherwise blank a cell with no error."""
    import re
    from tests.test_node_files import known_driver_fields
    published = set(known_driver_fields()["jbd"])
    for chart in _charts(CHINS):
        text = chart["queries"][0]["query"]
        named = set(re.findall(r'r\._field == "([a-z0-9_]+)"', text))
        assert named <= published, chart["name"]
        for pattern in re.findall(r"r\._field =~ /([^/]+)/", text):
            alternation = re.fullmatch(r"\^\(([a-z0-9_|]+)\)\$", pattern)
            if alternation:
                assert set(alternation.group(1).split("|")) <= published, chart["name"]
            else:
                assert any(re.search(pattern, field) for field in published), chart["name"]
