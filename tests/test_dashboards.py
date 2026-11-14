"""The committed energy dashboard is what its builder makes from the query.

The dashboard's daily cells are the daily energy query with a different
start. A dashboard edited by hand, or a query changed without rebuilding,
would show figures the documented query does not compute.
"""

import json
import pathlib
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
BUILD = REPO / "docs" / "dashboards" / "build.py"
DASHBOARD = REPO / "docs" / "dashboards" / "blind1-energy.json"


def _template():
    return json.loads(DASHBOARD.read_text())


def test_the_committed_dashboard_matches_its_builder():
    result = subprocess.run([sys.executable, str(BUILD), "--check"],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def _charts():
    (dashboard,) = _template()
    return dashboard["spec"]["charts"]


def test_the_dashboard_is_a_template_in_the_format_the_cloud_export_writes():
    (dashboard,) = _template()
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


def test_the_cells_tile_the_grid_without_overlapping():
    taken = set()
    for chart in _charts():
        x, y, w, h = chart["xPos"], chart["yPos"], chart["width"], chart["height"]
        assert 0 <= x and x + w <= 12
        for column in range(x, x + w):
            for row in range(y, y + h):
                assert (column, row) not in taken
                taken.add((column, row))
