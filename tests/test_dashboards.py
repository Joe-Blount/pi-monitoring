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
VICTRON = REPO / "docs" / "dashboards" / "blind1-victron.json"
SOK = REPO / "docs" / "dashboards" / "garage-sok.json"
INVERTER = REPO / "docs" / "dashboards" / "garage-charge-inverter.json"
BUILT = [DASHBOARD, HEALTH, CHINS, VICTRON, SOK, INVERTER]


def _template(path=DASHBOARD):
    return json.loads(path.read_text())


def test_the_committed_dashboard_matches_its_builder():
    result = subprocess.run([sys.executable, str(BUILD), "--check"],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def _charts(path=DASHBOARD):
    (dashboard,) = _template(path)
    return dashboard["spec"]["charts"]


@pytest.mark.parametrize("path", BUILT)
def test_the_dashboard_is_a_template_in_the_format_the_cloud_export_writes(path):
    (dashboard,) = _template(path)
    assert dashboard["apiVersion"] == "influxdata.com/v2alpha1"
    assert dashboard["kind"] == "Dashboard"
    for chart in dashboard["spec"]["charts"]:
        if chart["kind"] == "Markdown":
            assert chart["note"].strip()
            continue
        assert chart["kind"] in {"Xy", "Single_Stat", "Table"}
        assert chart["queries"][0]["query"].strip()


def test_the_daily_cells_follow_the_dashboards_time_range():
    daily = [c for c in _charts() if c["name"].startswith("Daily energy")]
    assert len(daily) == 3
    for chart in daily:
        text = chart["queries"][0]["query"]
        assert "v.timeRangeStart" in text
        assert "lookback" not in text


@pytest.mark.parametrize("path", BUILT)
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


def _assert_reads_only(path, published):
    """Every field a dashboard names is one its device publishes."""
    import re
    for chart in _charts(path):
        text = chart["queries"][0]["query"]
        named = set(re.findall(r'r\._field == "([a-z0-9_]+)"', text))
        assert named <= published, (chart["name"], named - published)
        for pattern in re.findall(r"r\._field =~ /([^/]+)/", text) + re.findall(
                r"fields: /([^/]+)/", text):
            alternation = re.fullmatch(r"\^\(([a-z0-9_|\[\]+-]+)\)\$", pattern)
            if alternation:
                for part in alternation.group(1).split("|"):
                    assert any(re.fullmatch(part, field) for field in published), (
                        chart["name"], part)
            else:
                assert any(re.search(pattern, field) for field in published), (
                    chart["name"], pattern)


def test_the_chins_dashboard_reads_only_fields_the_pack_publishes():
    """A renamed pack field would otherwise blank a cell with no error."""
    from tests.test_node_files import known_driver_fields
    _assert_reads_only(CHINS, set(known_driver_fields()["jbd"]))


def test_the_victron_dashboard_reads_only_fields_the_controller_publishes():
    from tests.test_node_files import known_driver_fields
    _assert_reads_only(VICTRON, set(known_driver_fields()["vedirect"]))


def test_the_sok_dashboard_reads_only_fields_the_packs_publish():
    from monitoring.drivers import abc_bms
    from tests import abc_bms_fixtures
    _assert_reads_only(SOK, set(abc_bms.parse(abc_bms_fixtures.real_messages())))


def test_the_inverter_dashboard_reads_only_fields_the_inverter_publishes():
    from monitoring.runner import RESERVED_FIELDS
    from tests.test_node_files import known_driver_fields
    _assert_reads_only(INVERTER, set(known_driver_fields()["eg4_modbus"])
                       | set(RESERVED_FIELDS))


def test_the_sok_alarm_words_follow_the_drivers_bit_order():
    """Bit N of alarm_flags is the Nth protection state the driver reads."""
    sys.path.insert(0, str(BUILD.parent))
    try:
        import build
    finally:
        sys.path.remove(str(BUILD.parent))
    from monitoring.drivers import abc_bms
    assert [code for code, _words in build.SOK_PROTECTION] == [
        code for code, _description in abc_bms.PROTECTION_STATES]


def test_the_load_estimates_name_every_figure_the_blind_controller_measures():
    """A figure with no name of its own shows as its bare load and
    condition, so a renamed or added load would otherwise pass unnoticed."""
    import yaml
    from monitoring.control import meter
    node = yaml.safe_load((REPO / "sites" / "blind1" / "downstairs.yaml").read_text())
    loads = {name: dict(load, pump={"expect_amps": (30.0, 60.0)})
             for name, load in node["control"]["loads"].items()}
    wired_around = set(node["control"]["meter"]["wired_around"])
    conditions = {"branch": ["night", "day"], "pump": ["wet", "dry", "high"],
                  "inverter": [], "other": [""]}
    rows = ["base "]
    for output, kind in meter.Meter(
            {"charge": [], "shunt": {}, "wired_around": []}, loads).kinds.items():
        if output.split(".")[0] in wired_around:
            continue
        rows += ["%s %s" % (output, c) for c in conditions[kind]]
    (chart,) = [c for c in _charts() if c["name"] == "Load estimates"]
    text = chart["queries"][0]["query"]
    for row in rows:
        assert 'r.row == "%s"' % row in text, row


@pytest.mark.parametrize("path", BUILT + sorted(
    (REPO / "docs" / "dashboards" / "cloud").glob("*.json")))
def test_no_table_fixes_its_first_column(path):
    """Cloud does not draw a fixed first column, so the column that names
    each row would be missing."""
    tables = [c for c in _charts(path) if c["kind"] == "Table"]
    for chart in tables:
        assert chart["tableOptions"]["fixFirstColumn"] is False, chart["name"]
