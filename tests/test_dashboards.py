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


def test_the_dashboard_is_in_the_format_the_web_importer_accepts():
    template = _template()
    assert template["meta"]["version"] == "1"
    assert template["content"]["data"]["type"] == "dashboard"
    included = {(item["type"], item["id"]): item
                for item in template["content"]["included"]}
    for ref in template["content"]["data"]["relationships"]["cell"]["data"]:
        cell = included[("cell", ref["id"])]
        assert set(cell["attributes"]) == {"x", "y", "w", "h"}
        view = included[("view", cell["relationships"]["view"]["data"]["id"])]
        properties = view["attributes"]["properties"]
        assert properties["shape"] == "chronograf-v2"
        assert properties["queries"][0]["text"].strip()


def test_the_daily_cells_follow_the_dashboards_time_range():
    views = [item["attributes"] for item in _template()["content"]["included"]
             if item["type"] == "view"]
    daily = [view for view in views if view["name"].startswith("Daily energy")]
    assert len(daily) == 3
    for view in daily:
        text = view["properties"]["queries"][0]["text"]
        assert "v.timeRangeStart" in text
        assert "lookback" not in text


def test_the_cells_tile_the_grid_without_overlapping():
    taken = set()
    for item in _template()["content"]["included"]:
        if item["type"] != "cell":
            continue
        a = item["attributes"]
        assert 0 <= a["x"] and a["x"] + a["w"] <= 12
        for x in range(a["x"], a["x"] + a["w"]):
            for y in range(a["y"], a["y"] + a["h"]):
                assert (x, y) not in taken
                taken.add((x, y))
