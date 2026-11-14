#!/usr/bin/env python3
"""Build the dashboards: the blind's energy, and every machine's health.

docs/queries/blind1-daily-energy.flux is the one place the daily figures
are worked out. The dashboard's daily cells are that query, started from the
dashboard's time range instead of a fixed lookback, so the two cannot
disagree. Run this after changing the query and commit what it writes:

    python3 docs/dashboards/build.py

With --check it writes nothing and exits 1 if the committed dashboard is out
of date.

The health dashboard finds machines by their host readings, so a machine
added later appears on it with no change here.

The output is an InfluxDB template, the format the web interface's own
dashboard export writes, and imports there: Dashboards, Create Dashboard,
Import Dashboard. The older export format with a meta block is accepted by
that dialog and then saves nothing in InfluxDB Cloud.
"""

import json
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
QUERY = HERE.parent / "queries" / "blind1-daily-energy.flux"
OUT = HERE / "blind1-energy.json"
HEALTH_OUT = HERE / "pi-health.json"

FIXED_START = "start = date.truncate(t: lookback, unit: 1d)\n"
PICKED_START = "start = date.truncate(t: v.timeRangeStart, unit: 1d)\n"
FINAL = "\nunion(\n"
NAMED_FINAL = "\ndaily = union(\n"

SUPPLY = """\
// Where the loads' power comes from, and where the surplus goes. Stacked,
// the two "loads from" series add up to the whole load. The top edge is the
// charge controller's output plus the battery's discharge. The shunt
// reports every minute and the controller every thirty seconds, so both are
// averaged over a common window of at least five minutes. A window missing
// either device is left out, and the lines are drawn straight across it.
// Heater draw crosses the shunt as charge and shows as surplus to the bank.

w = if int(v: v.windowPeriod) < int(v: 5m) then 5m else v.windowPeriod

node = (device) =>
    from(bucket: "raw")
        |> range(start: v.timeRangeStart, stop: v.timeRangeStop)
        |> filter(
            fn: (r) => r._measurement == "blind1" and r.node == "downstairs" and r.device == device,
        )

// The bank's power, split into charge and discharge reading by reading and
// only then averaged. A window that holds both would otherwise net them, and
// a cloud passing over would hide the battery's share.
bank = node(device: "shunt")
    |> filter(fn: (r) => r._field == "shunt_volts" or r._field == "shunt_amps")
    |> group()
    |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")
    |> filter(fn: (r) => exists r.shunt_volts and exists r.shunt_amps)
    |> map(fn: (r) => ({_time: r._time, watts: r.shunt_volts * r.shunt_amps}))

into = bank
    |> map(fn: (r) => ({_time: r._time, _field: "into", _value: if r.watts > 0.0 then r.watts else 0.0}))
    |> group(columns: ["_field"])
    |> aggregateWindow(every: w, fn: mean, createEmpty: false)

out = bank
    |> map(fn: (r) => ({_time: r._time, _field: "out", _value: if r.watts < 0.0 then 0.0 - r.watts else 0.0}))
    |> group(columns: ["_field"])
    |> aggregateWindow(every: w, fn: mean, createEmpty: false)

solar = node(device: "mppt")
    |> filter(fn: (r) => r._field == "battery_watts")
    |> group(columns: ["_field"])
    |> keep(columns: ["_time", "_field", "_value"])
    |> aggregateWindow(every: w, fn: mean, createEmpty: false)

both = union(tables: [into, out, solar])
    |> group()
    |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")
    |> filter(fn: (r) => exists r.into and exists r.out and exists r.battery_watts)

from_bank = both
    |> map(fn: (r) => ({_time: r._time, _field: "loads from battery", _value: r.out}))

from_solar = both
    |> map(
        fn: (r) => {
            direct = r.battery_watts - r.into

            return {_time: r._time, _field: "loads from solar",
                _value: if direct > 0.0 then direct else 0.0,
            }
        },
    )

to_bank = both
    |> map(fn: (r) => ({_time: r._time, _field: "surplus to bank", _value: r.into}))

union(tables: [from_bank, from_solar, to_bank])
    |> group(columns: ["_field"])
    |> sort(columns: ["_time"])
"""

SOC = """\
// The Chins pack's own state of charge, which the shedding ladder follows.

from(bucket: "raw")
    |> range(start: v.timeRangeStart, stop: v.timeRangeStop)
    |> filter(
        fn: (r) =>
            r._measurement == "blind1" and r.node == "downstairs" and r.device == "bms"
                and
                r._field == "state_of_charge_pct",
    )
    |> group()
    |> keep(columns: ["_time", "_value"])
    |> aggregateWindow(every: v.windowPeriod, fn: mean, createEmpty: false)
    |> map(fn: (r) => ({r with _field: "Chins state of charge"}))
"""

NIGHT = """\
// The bank's average draw while the charge controller produced nothing, over
// the last 24 hours whatever the dashboard's range. Night is a five-minute
// bin in which every controller reading shows no panel power, cut in UTC.
// A shunt step longer than two and a half poll intervals spans a gap. It is
// left out, and so is the time it covers.

import "date"
import "timezone"

bin = 5m
poll = 60s
start = -24h

shunt = from(bucket: "raw")
    |> range(start: -25h)
    |> filter(
        fn: (r) =>
            r._measurement == "blind1" and r.node == "downstairs" and r.device == "shunt"
                and
                (r._field == "cumulative_energy_wh" or r._field == "charging"),
    )
    |> group()
    |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")
    |> filter(fn: (r) => exists r.cumulative_energy_wh and exists r.charging)
    |> sort(columns: ["_time"])
    |> difference(columns: ["cumulative_energy_wh"], nonNegative: true, keepFirst: true)
    |> elapsed(unit: 1s)
    |> filter(
        fn: (r) =>
            exists r.cumulative_energy_wh and r._time >= date.add(d: start, to: now())
                and
                r.elapsed * 2 <= 5 * int(v: poll) / int(v: 1s),
    )
    |> map(
        fn: (r) =>
            ({
                bin: date.truncate(t: r._time, unit: bin, location: timezone.utc),
                wh: if r.charging > 0.5 then 0.0 else r.cumulative_energy_wh,
                secs: float(v: r.elapsed),
            }),
    )

dark = from(bucket: "raw")
    |> range(start: start)
    |> filter(
        fn: (r) =>
            r._measurement == "blind1" and r.node == "downstairs" and r.device == "mppt"
                and
                r._field == "pv_watts",
    )
    |> group()
    |> map(fn: (r) => ({bin: date.truncate(t: r._time, unit: bin, location: timezone.utc), pv: r._value}))
    |> group(columns: ["bin"])
    |> max(column: "pv")
    |> group()
    |> filter(fn: (r) => r.pv <= 0.0)
    |> keep(columns: ["bin"])

join(tables: {s: shunt, d: dark}, on: ["bin"])
    |> reduce(
        identity: {wh: 0.0, secs: 0.0},
        fn: (r, accumulator) => ({wh: accumulator.wh + r.wh, secs: accumulator.secs + r.secs}),
    )
    |> map(fn: (r) => ({_value: r.wh * 3600.0 / r.secs}))
"""


def daily(query, tail):
    """The daily query, started from the dashboard's range, with `tail`.

    `tail` is Flux that reads the query's result as `daily`. With no tail the
    query is returned as it stands, for a table.
    """
    if FIXED_START not in query or FINAL not in query:
        raise SystemExit(
            "%s no longer has the start line or the final union this builder "
            "rewrites" % QUERY)
    query = query.replace(FIXED_START, PICKED_START)
    query = query.replace(
        "// How far back. Truncated to local midnight so the first day is whole. A day\n"
        "// short of thirty, so the first day stays inside a bucket kept for thirty.\n"
        "lookback = -29d\n\n", "")
    if tail is None:
        return query
    return query.replace(FINAL, NAMED_FINAL) + "\n" + tail


def steps(pairs):
    """Daily totals as flat lines, one per field, a day wide each.

    Each day is drawn from its midnight to a second before the next, so
    adjacent days meet in a vertical step and read like bars. Ending a
    second early keeps a day's end ahead of the next day's start once sorted.
    """
    series = ",\n        ".join(
        'daily |> map(fn: (r) => ({day: r.day, _field: "%s", _value: r.%s}))'
        % (label, column) for label, column in pairs)
    return """\
picked = union(
    tables: [
        %s,
    ],
)

union(
    tables: [
        picked |> map(fn: (r) => ({r with _time: r.day})),
        picked |> map(fn: (r) => ({r with _time: date.sub(d: 1s, from: date.add(d: 1d, to: r.day))})),
    ],
)
    |> drop(columns: ["day"])
    |> group(columns: ["_field"])
    |> sort(columns: ["_time"])
""" % series


PALETTE = [
    {"name": "Nineteen Eighty Four", "type": "scale", "hex": "#31C0F6"},
    {"name": "Nineteen Eighty Four", "type": "scale", "hex": "#A500A5"},
    {"name": "Nineteen Eighty Four", "type": "scale", "hex": "#FF7E27"},
]

TEXT = [{"name": "laser", "type": "text", "hex": "#00C9FF"}]


def xy(name, text, unit, position="overlaid"):
    y = {"name": "y", "label": unit, "base": "10", "scale": "linear"}
    return {
        "name": name,
        "kind": "Xy",
        "queries": [{"query": text}],
        "geom": "line",
        "xCol": "_time",
        "yCol": "_value",
        "position": position,
        "colors": PALETTE,
        "axes": [{"name": "x", "base": "10", "scale": "linear"}, y],
    }


def single(name, text, suffix):
    return {
        "name": name,
        "kind": "Single_Stat",
        "queries": [{"query": text}],
        "suffix": suffix,
        "decimalPlaces": 0,
        "colors": TEXT,
    }


TABLE_COLUMNS = [
    "day", "bank_in_wh", "bank_out_wh", "bank_net_wh", "ah_net", "solar_wh",
    "load_wh", "direct_solar_wh", "night_load_wh", "night_hours",
    "night_avg_w", "solar_wh_check", "shunt_coverage_pct",
]


def table(name, text):
    return {
        "name": name,
        "kind": "Table",
        "queries": [{"query": text}],
        "timeFormat": "YYYY-MM-DD",
        "decimalPlaces": 0,
        "enforceDecimals": True,
        "colors": TEXT,
        "tableOptions": {
            "verticalTimeAxis": True,
            "wrapping": "truncate",
            "fixFirstColumn": True,
            "sortBy": "day",
        },
        "fieldOptions": [{"fieldName": c, "displayName": c, "visible": True}
                         for c in TABLE_COLUMNS],
    }


def dashboard(meta_name, name, description, cells):
    """An InfluxDB template holding one dashboard.

    `cells` is (x, y, w, h, chart) on the dashboard's twelve-column grid.
    """
    charts = []
    for x, y, w, h, chart in cells:
        placed = {"name": chart["name"], "xPos": x, "yPos": y,
                  "width": w, "height": h}
        placed.update((k, v) for k, v in chart.items() if k != "name")
        charts.append(placed)
    return [{
        "apiVersion": "influxdata.com/v2alpha1",
        "kind": "Dashboard",
        "metadata": {"name": meta_name},
        "spec": {"name": name, "description": description, "charts": charts},
    }]


def build():
    query = QUERY.read_text()
    # (x, y, w, h, chart) on the dashboard's twelve-column grid.
    cells = [
        (0, 0, 9, 4, xy("Where the load's power comes from", SUPPLY, "W",
                        position="stacked")),
        (9, 0, 3, 6, single("Night draw, last 24 hours", NIGHT, " W")),
        (0, 4, 9, 2, xy("Chins state of charge", SOC, "%")),
        (0, 6, 6, 3, xy("Daily energy, bank",
                        daily(query, steps([("bank in", "bank_in_wh"),
                                            ("bank out", "bank_out_wh")])),
                        "Wh")),
        (6, 6, 6, 3, xy("Daily energy, system",
                        daily(query, steps([("solar", "solar_wh"),
                                            ("load", "load_wh")])),
                        "Wh")),
        (0, 9, 12, 4, table("Daily energy", daily(query, None))),
    ]
    return dashboard(
        "blind-energy", "Blind energy",
        "The blind's battery bank and solar, from the shunt and the charge "
        "controller.", cells)


# Every machine is found by the fields its host device publishes, never by
# name, so the dashboard needs no edit when a machine is added. Each series
# is one machine, labelled by its measurement (the site) and node.

def health_graph(field):
    return """\
from(bucket: "raw")
    |> range(start: v.timeRangeStart, stop: v.timeRangeStop)
    |> filter(fn: (r) => r._field == "%s")
    |> group(columns: ["_measurement", "node"])
    |> aggregateWindow(every: v.windowPeriod, fn: mean, createEmpty: false)
""" % field


MACHINES = """\
// One row per machine that has reported in the last week, newest readings.
// A machine that stops reporting stays listed, and "seen" says how long ago.

import "math"

// Strings and numbers are read apart: one stream holding both is a type
// conflict for every function that touches _value.
recent = (fields) =>
    from(bucket: "raw")
        |> range(start: -7d)
        |> filter(fn: (r) => contains(value: r._field, set: fields))
        |> last()

latest = recent(
    fields: [
        "uptime_seconds",
        "cpu_temp_f",
        "load_per_core",
        "mem_used_pct",
        "swap_used_pct",
        "root_used_pct",
        "code_dirty",
        "code_installed_time",
    ],
)

ago = (t) => string(v: duration(v: (int(v: now()) - int(v: t)) / 1000000000 * 1000000000))

seen = latest
    |> filter(fn: (r) => r._field == "uptime_seconds")
    |> map(fn: (r) => ({r with _field: "seen", _value: ago(t: r._time) + " ago"}))

uptime = latest
    |> filter(fn: (r) => r._field == "uptime_seconds")
    |> map(fn: (r) => ({r with _field: "up", _value: string(v: duration(v: int(v: r._value) * 1000000000))}))

numbers = latest
    |> filter(
        fn: (r) =>
            r._field == "cpu_temp_f" or r._field == "load_per_core" or r._field == "mem_used_pct"
                or
                r._field == "swap_used_pct" or r._field == "root_used_pct",
    )
    |> map(fn: (r) => ({r with _value: string(v: math.round(x: r._value * 100.0) / 100.0)}))

text = recent(fields: ["ip", "code_commit", "code_branch"])

dirty = latest
    |> filter(fn: (r) => r._field == "code_dirty")
    |> map(fn: (r) => ({r with _value: if r._value > 0.5 then "local changes" else "clean"}))

installed = latest
    |> filter(fn: (r) => r._field == "code_installed_time")
    |> map(
        fn: (r) =>
            ({r with _value: ago(t: time(v: int(v: r._value) * 1000000000)) + " ago"}),
    )

union(tables: [seen, uptime, numbers, text, dirty, installed])
    |> group(columns: ["_measurement", "node"])
    |> pivot(rowKey: ["_measurement", "node"], columnKey: ["_field"], valueColumn: "_value")
    |> group()
    |> rename(columns: {_measurement: "site"})
    |> sort(columns: ["site", "node"])
"""

MACHINE_COLUMNS = [
    ("site", "site"), ("node", "node"), ("seen", "seen"), ("up", "up"),
    ("ip", "address"), ("cpu_temp_f", "CPU F"), ("load_per_core", "load/core"),
    ("mem_used_pct", "mem %"), ("swap_used_pct", "swap %"),
    ("root_used_pct", "disk %"), ("code_commit", "commit"),
    ("code_branch", "branch"), ("code_dirty", "checkout"),
    ("code_installed_time", "installed"),
]


def machines_table():
    chart = table("Machines", MACHINES)
    chart["timeFormat"] = ""
    chart["tableOptions"]["sortBy"] = "site"
    chart["fieldOptions"] = [
        {"fieldName": field, "displayName": label, "visible": True}
        for field, label in MACHINE_COLUMNS]
    return chart


def build_health():
    cells = [
        (0, 0, 12, 3, machines_table()),
        (0, 3, 6, 3, xy("CPU temperature", health_graph("cpu_temp_f"), "F")),
        (6, 3, 6, 3, xy("Load per core", health_graph("load_per_core"), "")),
        (0, 6, 4, 3, xy("Memory used", health_graph("mem_used_pct"), "%")),
        (4, 6, 4, 3, xy("Swap used", health_graph("swap_used_pct"), "%")),
        (8, 6, 4, 3, xy("Disk used", health_graph("root_used_pct"), "%")),
        (0, 9, 12, 2, xy("Uptime", health_graph("uptime_seconds"), "s")),
    ]
    return dashboard(
        "pi-health", "Pi health",
        "Every machine that reports host readings, found by its readings.",
        cells)


def main(argv):
    outputs = [(OUT, build()), (HEALTH_OUT, build_health())]
    if argv[1:] == ["--check"]:
        stale = [path for path, template in outputs
                 if not path.exists()
                 or path.read_text() != json.dumps(template, indent=2) + "\n"]
        for path in stale:
            print("%s is out of date; run %s" % (path, __file__), file=sys.stderr)
        return 1 if stale else 0
    if argv[1:]:
        print("usage: %s [--check]" % argv[0], file=sys.stderr)
        return 2
    for path, template in outputs:
        path.write_text(json.dumps(template, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
