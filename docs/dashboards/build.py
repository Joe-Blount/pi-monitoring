#!/usr/bin/env python3
"""Build the dashboards: the blind's energy, Chins pack and charge controller,
the garage's packs and inverter, and every machine's health.

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
LOADS_QUERY = HERE.parent / "queries" / "blind1-loads.flux"
OUT = HERE / "blind1-energy.json"
HEALTH_OUT = HERE / "pi-health.json"
CHINS_OUT = HERE / "blind1-chins.json"
VICTRON_OUT = HERE / "blind1-victron.json"
SOK_OUT = HERE / "garage-sok.json"
INVERTER_OUT = HERE / "garage-charge-inverter.json"

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
// The Chins pack's own state of charge, which the shedding ladder follows,
// and the shunt's. The shunt counts amp-hours for the whole bank and drifts
// from the packs between full charges.

from(bucket: "raw")
    |> range(start: v.timeRangeStart, stop: v.timeRangeStop)
    |> filter(
        fn: (r) =>
            r._measurement == "blind1" and r.node == "downstairs"
                and
                (r.device == "bms" or r.device == "shunt")
                and
                r._field == "state_of_charge_pct",
    )
    |> group(columns: ["device"])
    |> aggregateWindow(every: v.windowPeriod, fn: mean, createEmpty: false)
    |> map(fn: (r) => ({_time: r._time, _value: r._value, _field: if r.device == "bms" then "Chins" else "shunt"}))
    |> group(columns: ["_field"])
"""

# The Chins reports its own current and the shunt the whole bank's. The
# Weize pair has no meter of its own, so its share is the difference.
PACK_AMPS = """\
// The two devices read at different moments, so both are averaged over a
// common window of at least three minutes. A window missing either is left
// out.

w = if int(v: v.windowPeriod) < int(v: 3m) then 3m else v.windowPeriod

amps = from(bucket: "raw")
    |> range(start: v.timeRangeStart, stop: v.timeRangeStop)
    |> filter(
        fn: (r) =>
            r._measurement == "blind1" and r.node == "downstairs"
                and
                (r.device == "bms" and r._field == "pack_amps" or r.device == "shunt" and r._field == "shunt_amps"),
    )
    |> group(columns: ["_field"])
    |> aggregateWindow(every: w, fn: mean, createEmpty: false)
    |> group()
    |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")
    |> filter(fn: (r) => exists r.pack_amps and exists r.shunt_amps)
"""

PACK_CURRENT = PACK_AMPS + """
union(
    tables: [
        amps |> map(fn: (r) => ({_time: r._time, _field: "whole bank", _value: r.shunt_amps})),
        amps |> map(fn: (r) => ({_time: r._time, _field: "Chins", _value: r.pack_amps})),
        amps |> map(fn: (r) => ({_time: r._time, _field: "Weize pair", _value: r.shunt_amps - r.pack_amps})),
    ],
)
    |> group(columns: ["_field"])
    |> sort(columns: ["_time"])
"""

PACK_SHARE = """\
// Each pack's share of the bank's current, in percent. A share of almost no
// current swings wildly, so a window is left out while the bank carries less
// than two amps either way. "Chins, by day" is the Chins's share of the
// day's throughput, charge and discharge alike, drawn a day wide. A share
// above 100 or below 0 means one pack is charging the other.

import "date"
import "math"
import "timezone"

option location = timezone.location(name: "America/Chicago")

""" + PACK_AMPS + """
moving = amps |> filter(fn: (r) => math.abs(x: r.shunt_amps) >= 2.0)

// The day's throughput from each reading's size, taken before any averaging,
// so charge and discharge add rather than cancel. The sizes are averaged over
// three-minute windows and only windows holding both devices count, so a gap
// in one device does not tilt the day. Three minutes divides the day, and
// each window counts on the day it starts, so no window crosses midnight and
// a whole day's figure does not change with the dashboard's zoom. A day the
// range cuts holds only the part inside the range.
daily = from(bucket: "raw")
    |> range(start: v.timeRangeStart, stop: v.timeRangeStop)
    |> filter(
        fn: (r) =>
            r._measurement == "blind1" and r.node == "downstairs"
                and
                (r.device == "bms" and r._field == "pack_amps" or r.device == "shunt" and r._field == "shunt_amps"),
    )
    |> map(fn: (r) => ({r with _value: math.abs(x: r._value)}))
    |> group(columns: ["_field"])
    |> aggregateWindow(every: 3m, fn: mean, createEmpty: false, timeSrc: "_start")
    |> group()
    |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")
    |> filter(fn: (r) => exists r.pack_amps and exists r.shunt_amps)
    |> map(fn: (r) => ({day: date.truncate(t: r._time, unit: 1d), chins: r.pack_amps, bank: r.shunt_amps}))
    |> group(columns: ["day"])
    |> reduce(
        identity: {chins: 0.0, bank: 0.0},
        fn: (r, accumulator) => ({chins: accumulator.chins + r.chins, bank: accumulator.bank + r.bank}),
    )
    |> group()
    |> filter(fn: (r) => r.bank > 0.0)
    |> map(fn: (r) => ({day: r.day, _value: 100.0 * r.chins / r.bank}))

union(
    tables: [
        moving |> map(fn: (r) => ({_time: r._time, _field: "Chins", _value: 100.0 * r.pack_amps / r.shunt_amps})),
        moving
            |> map(fn: (r) => ({_time: r._time, _field: "Weize pair", _value: 100.0 - 100.0 * r.pack_amps / r.shunt_amps})),
        daily |> map(fn: (r) => ({_time: r.day, _field: "Chins, by day", _value: r._value})),
        daily
            |> map(
                fn: (r) =>
                    ({_time: date.sub(d: 1s, from: date.add(d: 1d, to: r.day)), _field: "Chins, by day", _value: r._value}),
            ),
    ],
)
    |> group(columns: ["_field"])
    |> sort(columns: ["_time"])
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


def single(name, text, suffix, decimals=0, colors=None):
    return {
        "name": name,
        "kind": "Single_Stat",
        "queries": [{"query": text}],
        "suffix": suffix,
        "decimalPlaces": decimals,
        "colors": colors or TEXT,
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
            # Cloud does not draw a fixed first column at all.
            "fixFirstColumn": False,
            "sortBy": "day",
        },
        "fieldOptions": [{"fieldName": c, "displayName": c, "visible": True}
                         for c in TABLE_COLUMNS],
    }


def note(name, text):
    return {"name": name, "kind": "Markdown", "note": text}


LOADS_COLUMNS = [
    ("load", "load"),
    ("amps", "A"),
    ("watts_at_13v2", "W"),
    ("measured", "measured"),
]

LOADS_NOTE = """\
The controller measures each load as the step in the bank's current when \
it switches that load, with no charge coming in and nothing else \
switching. It keeps the latest figure for each and publishes it on every \
pass, so a figure stays in the table until a newer one replaces it.

The cameras are measured at night and by day apart, because their \
infrared lights draw at night. A switch in twilight is not measured. The \
far cameras start five minutes after the near ones, so each group's start \
is a step of its own.

**Base load** is the bank's mean draw over the ten minutes after every \
relay load has been off for ten. The recorder is wired around its relay, so it is part of \
the base load.

The pump is measured as it starts, with the inverter that starts just \
before it.

Values from tests made by hand:

| Load | A | Measured |
|---|---|---|
| Pump moving water, with inverter | 46.4 | 2026-11-14, controller run |
| Pump running dry, with inverter | 17.4 | 2026-11-14, controller run |
| Far cameras by day | 2.0 | 2026-11-14, walk-down |
| Near cameras by day | 2.4 | 2026-11-14, walk-down, hand reading |
| Base load by day | 1.6 | 2026-11-14, walk-down, hand reading |
| Far cameras at night | 2.9 | 2026-11-14, walk-down |
| Near cameras at night | 3.4 | 2026-11-14, walk-down |
| Base load at night | 1.8 | 2026-11-14, walk-down |
"""


def loads_table():
    chart = status_table("Load estimates", LOADS_QUERY.read_text(), "load",
                         LOADS_COLUMNS)
    chart["decimalPlaces"] = 2
    chart["timeFormat"] = "YYYY-MM-DD HH:mm"
    return chart


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
        (0, 4, 9, 2, xy("State of charge", SOC, "%")),
        (0, 6, 6, 3, xy("Daily energy, bank",
                        daily(query, steps([("bank in", "bank_in_wh"),
                                            ("bank out", "bank_out_wh")])),
                        "Wh")),
        (6, 6, 6, 3, xy("Daily energy, system",
                        daily(query, steps([("solar", "solar_wh"),
                                            ("load", "load_wh")])),
                        "Wh")),
        (0, 9, 12, 4, table("Daily energy", daily(query, None))),
        (0, 13, 6, 3, xy("Current by pack (positive is charging)", PACK_CURRENT,
                         "A")),
        (6, 13, 6, 3, xy("Share of the bank's current", PACK_SHARE, "%")),
        (0, 16, 7, 7, loads_table()),
        (7, 16, 5, 7, note("How loads are measured", LOADS_NOTE)),
    ]
    return dashboard(
        "blind-energy", "Blind energy",
        "The blind's battery bank and solar, from the shunt and the charge "
        "controller.", cells)


# Every machine is found by the fields its host device publishes, never by
# name, so the dashboard needs no edit when a machine is added. Each series
# is one machine, labelled by its measurement (the site) and node.

def health_graph(field, divisor=None):
    """One line per machine, the field divided by `divisor` when given."""
    convert = ("" if divisor is None else
               "    |> map(fn: (r) => ({r with _value: r._value / %s}))\n" % divisor)
    return """\
from(bucket: "raw")
    |> range(start: v.timeRangeStart, stop: v.timeRangeStop)
    |> filter(fn: (r) => r._field == "%s")
    |> group(columns: ["_measurement", "node"])
    |> aggregateWindow(every: v.windowPeriod, fn: mean, createEmpty: false)
%s""" % (field, convert)


MACHINES = """\
// One row per machine that has reported in the last week, newest readings.
// A machine that stops reporting stays listed, and "seen" says how long ago.

import "math"

// Strings and numbers are read apart: one stream holding both is a type
// conflict for every function that touches _value. Each filter is a plain
// comparison, so storage applies it and last() before anything is read.
latest = from(bucket: "raw")
    |> range(start: -7d)
    |> filter(
        fn: (r) =>
            r.device == "host"
                and
                r._field =~ /^(uptime_seconds|cpu_temp_f|load_per_core|mem_used_pct|swap_used_pct|root_used_pct|code_dirty|code_installed_time)$/,
    )
    |> last()

text = from(bucket: "raw")
    |> range(start: -7d)
    |> filter(fn: (r) => r.device == "host" and r._field =~ /^(ip|code_commit|code_branch)$/)
    |> last()

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

dirty = latest
    |> filter(fn: (r) => r._field == "code_dirty")
    |> map(
        fn: (r) =>
            ({r with
                _value: if r._value > 0.5 then "local changes" else if r._value < -0.5 then "unknown" else "clean",
            }),
    )

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
        # A graph axis has one unit, so uptime is drawn in days; the table
        # gives it as days, hours and minutes.
        (0, 9, 12, 2, xy("Uptime", health_graph("uptime_seconds", "86400.0"),
                         "days")),
    ]
    return dashboard(
        "pi-health", "Pi health",
        "Every machine that reports host readings, found by its readings.",
        cells)


# The blind's Chins pack, read by the downstairs node's bms device. The cell
# and temperature graphs match fields by pattern, so a pack with more cells
# or sensors fills them with no change here.

def chins(body):
    return """\
from(bucket: "raw")
    |> range(start: v.timeRangeStart, stop: v.timeRangeStop)
    |> filter(fn: (r) => r._measurement == "blind1" and r.node == "downstairs" and r.device == "bms")
%s""" % body


def chins_graph(pattern, scale=None):
    convert = ("" if scale is None else
               "    |> map(fn: (r) => ({r with _value: r._value * %s}))\n" % scale)
    return chins("""\
    |> filter(fn: (r) => r._field =~ /%s/)
    |> aggregateWindow(every: v.windowPeriod, fn: mean, createEmpty: false)
%s""" % (pattern, convert))


def chins_latest(field, scale=None):
    convert = ("" if scale is None else
               "    |> map(fn: (r) => ({r with _value: r._value * %s}))\n" % scale)
    return """\
// The newest reading in the last hour. Empty when the pack has not answered
// for an hour, rather than an old number shown as current.
from(bucket: "raw")
    |> range(start: -1h)
    |> filter(
        fn: (r) =>
            r._measurement == "blind1" and r.node == "downstairs" and r.device == "bms"
                and
                r._field == "%s",
    )
    |> last()
%s""" % (field, convert)


#: The protection word's bits, as the JBD protocol numbers them from 0.
PROTECTION_BITS = [
    "cell over volts", "cell under volts", "pack over volts",
    "pack under volts", "charge too hot", "charge too cold",
    "discharge too hot", "discharge too cold", "charge over current",
    "discharge over current", "short circuit", "front end chip error",
    "switches locked",
]

#: Cells the balancing word can name: bit N is cell N + 1. As many as the
#: driver accepts.
BALANCING_CELLS = 32


def bit_names(names):
    """Flux naming every set bit of `v`, comma separated.

    Bits above the named ones are shown as their value, so an alarm with no
    name is never an empty cell.
    """
    parts = [
        '(if v / %d %% 2 == 1 then "%s, " else "")' % (1 << bit, name)
        for bit, name in enumerate(names)]
    top = 1 << len(names)
    parts.append('(if v >= %d then "other bits " + string(v: v - v %% %d) + ", " else "")'
                 % (top, top))
    parts = " +\n        ".join(parts)
    return "strings.trimSuffix(\n    v:\n        %s,\n    suffix: \", \",\n)" % parts


CHINS_STATUS = """\
// The pack's own switches and alarms, newest reading in the last day. Each
// field is its own latest value; "read" is how long ago the pack last
// answered at all. Protection names each alarm the pack has raised, and
// balancing names each cell it is bleeding.

import "strings"

protection = (v) =>
    %s

balancing = (v) =>
    %s

ago = (t) => string(v: duration(v: (int(v: now()) - int(v: t)) / 1000000000 * 1000000000))

latest = from(bucket: "raw")
    |> range(start: -1d)
    |> filter(
        fn: (r) =>
            r._measurement == "blind1" and r.node == "downstairs" and r.device == "bms"
                and
                r._field =~ /^(charge_mosfet_on|discharge_mosfet_on|protection_flags|balancing_flags|commands_unanswered)$/,
    )
    |> last()

states = latest
    |> map(
        fn: (r) =>
            ({r with
                _value:
                    if r._field =~ /mosfet/ then
                        if r._value > 0.5 then "on" else "OFF"
                    else if r._value == 0.0 and r._field =~ /flags/ then
                        "none"
                    else if r._field == "protection_flags" then
                        protection(v: int(v: r._value))
                    else if r._field == "balancing_flags" then
                        "cells " + balancing(v: int(v: r._value))
                    else
                        string(v: int(v: r._value)),
            }),
    )

read = latest
    |> group()
    |> max(column: "_time")
    |> map(fn: (r) => ({r with _field: "read", _value: ago(t: r._time) + " ago"}))

union(tables: [states |> group(), read])
    |> keep(columns: ["_measurement", "_field", "_value"])
    |> pivot(rowKey: ["_measurement"], columnKey: ["_field"], valueColumn: "_value")
""" % (bit_names(PROTECTION_BITS),
       bit_names([str(cell + 1) for cell in range(BALANCING_CELLS)]))

CHINS_STATUS_COLUMNS = [
    ("read", "read"), ("charge_mosfet_on", "charging"),
    ("discharge_mosfet_on", "discharging"),
    ("protection_flags", "protection"), ("balancing_flags", "balancing"),
    ("commands_unanswered", "unanswered"),
]

#: Background colours by state of charge: red under 25, amber under 50, green
#: above. The bands are the camera ladder's 25 and 50 percent tiers in
#: sites/blind1/downstairs.yaml, copied rather than read, so they need
#: changing by hand if those tiers move.
SOC_COLORS = [
    {"name": "base", "type": "background", "hex": "#DC4E58", "value": 0},
    {"name": "c", "type": "background", "hex": "#FFB94A", "value": 25},
    {"name": "c", "type": "background", "hex": "#34BB55", "value": 50},
]


def chins_status_table():
    chart = table("Pack status", CHINS_STATUS)
    chart["timeFormat"] = ""
    chart["tableOptions"]["sortBy"] = "read"
    chart["fieldOptions"] = [
        {"fieldName": field, "displayName": label, "visible": True}
        for field, label in CHINS_STATUS_COLUMNS]
    return chart


def build_chins():
    cells = [
        (0, 0, 2, 2, single("State of charge",
                            chins_latest("state_of_charge_pct"), "%",
                            colors=SOC_COLORS)),
        (2, 0, 2, 2, single("Pack", chins_latest("pack_volts"), " V", 2)),
        (4, 0, 2, 2, single("Current", chins_latest("pack_amps"), " A", 1)),
        (6, 0, 2, 2, single("Cell spread", chins_latest("cell_spread_volts", "1000.0"),
                            " mV")),
        (8, 0, 2, 2, single("Temperature", chins_latest("temp_1"), " F", 1)),
        (10, 0, 2, 2, single("Cycles", chins_latest("cycles"), "")),
        (0, 2, 12, 2, chins_status_table()),
        (0, 4, 6, 4, xy("Cell voltages", chins_graph("^cell_[0-9]+_volts$"), "V")),
        (6, 4, 6, 4, xy("Cell spread", chins_graph("^cell_spread_volts$", "1000.0"),
                        "mV")),
        (0, 8, 6, 3, xy("Current (positive is charging)", chins_graph("^pack_amps$"),
                        "A")),
        (6, 8, 6, 3, xy("State of charge", chins_graph("^state_of_charge_pct$"), "%")),
        (0, 11, 6, 3, xy("Remaining capacity", chins_graph("^remaining_capacity_ah$"),
                         "Ah")),
        (6, 11, 6, 3, xy("Temperature", chins_graph("^temp_[0-9]+$"), "F")),
    ]
    return dashboard(
        "blind1-chins", "Blind1 Chins",
        "The blind's Chins pack, from its own battery management system.",
        cells)


# The blind's charge controller. Its readings are matched by device and not
# by node, so the controller stays on the dashboard whichever node reads it.

def victron(body):
    return """\
from(bucket: "raw")
    |> range(start: v.timeRangeStart, stop: v.timeRangeStop)
    |> filter(fn: (r) => r._measurement == "blind1" and r.device == "mppt")
%s""" % body


def victron_graph(field):
    return victron("""\
    |> filter(fn: (r) => r._field == "%s")
    |> group(columns: ["_field"])
    |> aggregateWindow(every: v.windowPeriod, fn: mean, createEmpty: false)
""" % field)


def victron_latest(field):
    return """\
// The newest reading in the last ten minutes. The controller reports every
// thirty seconds, so an empty cell means it stopped, not that it is idle.
from(bucket: "raw")
    |> range(start: -10m)
    |> filter(fn: (r) => r._measurement == "blind1" and r.device == "mppt" and r._field == "%s")
    |> group()
    |> sort(columns: ["_time"])
    |> last()
""" % field


#: VE.Direct error numbers, in words. A number not listed is shown as itself.
VICTRON_ERRORS = [
    (0, "none"), (2, "battery volts too high"), (17, "charger too hot"),
    (18, "charger over current"), (19, "charger current reversed"),
    (20, "bulk time limit reached"), (21, "current sensor fault"),
    (26, "terminals too hot"), (28, "converter fault"),
    (33, "panel volts too high"), (34, "panel current too high"),
    (38, "input off: battery volts too high"),
    (39, "input off: current while switched off"),
    (65, "lost contact with a networked device"),
    (66, "synchronised charging misconfigured"), (67, "BMS connection lost"),
    (68, "network misconfigured"), (116, "calibration data lost"),
    (117, "firmware incompatible"), (119, "settings invalid"),
]

#: The off reason's bits, numbered from 0.
VICTRON_OFF_BITS = [
    "no panel power", "power switch off", "switched off in settings",
    "remote input", "protection", "pay as you go", "BMS",
    "engine shutdown detected", "checking panel volts",
]


def victron_errors():
    chain = "".join('if v == %d then "%s"\n        else ' % (code, words)
                    for code, words in VICTRON_ERRORS)
    return chain + '"error " + string(v: v)'


VICTRON_STATUS = """\
// What the controller says it is doing, newest reading in the last day.
// "read" is how long ago it last reported. Words and numbers are read apart,
// since one stream holding both is a type conflict.

import "strings"

errors = (v) =>
    %s

off = (v) =>
    %s

ago = (t) => string(v: duration(v: (int(v: now()) - int(v: t)) / 1000000000 * 1000000000))

mppt = (fields) =>
    from(bucket: "raw")
        |> range(start: -1d)
        |> filter(fn: (r) => r._measurement == "blind1" and r.device == "mppt" and r._field =~ fields)

words = mppt(fields: /^(charge_state|tracker)$/)
    |> group(columns: ["_field"])
    |> sort(columns: ["_time"])
    |> last()
    |> map(
        fn: (r) =>
            ({r with
                _value:
                    if r._value == "mppt_active" then
                        "tracking"
                    else
                        strings.replaceAll(v: r._value, t: "_", u: " "),
            }),
    )

codes = mppt(fields: /^(error|off_reason_code)$/)
    |> group(columns: ["_field"])
    |> sort(columns: ["_time"])
    |> last()
    |> map(
        fn: (r) =>
            ({r with
                _value:
                    if r._field == "error" then
                        errors(v: int(v: r._value))
                    else if r._value == 0.0 then
                        "none"
                    else
                        off(v: int(v: r._value)),
            }),
    )

states = union(tables: [words |> group(), codes |> group()])

read = states
    |> max(column: "_time")
    |> map(fn: (r) => ({r with _field: "read", _value: ago(t: r._time) + " ago"}))

union(tables: [states, read])
    |> keep(columns: ["_measurement", "_field", "_value"])
    |> pivot(rowKey: ["_measurement"], columnKey: ["_field"], valueColumn: "_value")
"""

VICTRON_STATUS_COLUMNS = [
    ("read", "read"), ("charge_state", "charging"), ("tracker", "tracker"),
    ("error", "error"), ("off_reason_code", "off because"),
]

VICTRON_DAILY = """\
// The controller's yield by local day, from its lifetime total, the way the
// daily energy query works out solar_wh. Each day is drawn from its midnight
// to a second before the next.

import "date"
import "timezone"

option location = timezone.location(name: "America/Chicago")

start = date.truncate(t: v.timeRangeStart, unit: 1d)

daily = from(bucket: "raw")
    |> range(start: date.sub(d: 1h, from: start), stop: v.timeRangeStop)
    |> filter(fn: (r) => r._measurement == "blind1" and r.device == "mppt" and r._field == "yield_total_kwh")
    |> group()
    |> sort(columns: ["_time"])
    |> difference(nonNegative: true)
    |> filter(fn: (r) => exists r._value and r._time >= start)
    |> map(fn: (r) => ({day: date.truncate(t: r._time, unit: 1d), kwh: r._value}))
    |> group(columns: ["day"])
    |> sum(column: "kwh")
    |> group()

union(
    tables: [
        daily |> map(fn: (r) => ({_time: r.day, _field: "yield", _value: r.kwh})),
        daily |> map(fn: (r) => ({_time: date.sub(d: 1s, from: date.add(d: 1d, to: r.day)), _field: "yield", _value: r.kwh})),
    ],
)
    |> group(columns: ["_field"])
    |> sort(columns: ["_time"])
"""


def status_table(name, text, row, columns):
    chart = table(name, text)
    chart["timeFormat"] = ""
    chart["tableOptions"]["sortBy"] = row
    chart["fieldOptions"] = [
        {"fieldName": field, "displayName": label, "visible": True}
        for field, label in columns]
    return chart


def build_victron():
    status = VICTRON_STATUS % (victron_errors(), bit_names(VICTRON_OFF_BITS))
    cells = [
        (0, 0, 2, 2, single("Battery", victron_latest("battery_volts"), " V", 2)),
        (2, 0, 2, 2, single("Charge current", victron_latest("battery_amps"), " A", 1)),
        (4, 0, 2, 2, single("Panels", victron_latest("pv_watts"), " W")),
        (6, 0, 2, 2, single("Today", victron_latest("yield_today_kwh"), " kWh", 2)),
        (8, 0, 2, 2, single("Peak today", victron_latest("max_power_today_watts"), " W")),
        (10, 0, 2, 2, single("Yesterday", victron_latest("yield_yesterday_kwh"), " kWh", 2)),
        (0, 2, 12, 2, status_table("Controller status", status, "read",
                                   VICTRON_STATUS_COLUMNS)),
        (0, 4, 4, 3, xy("Battery volts", victron_graph("battery_volts"), "V")),
        (4, 4, 4, 3, xy("Charge current", victron_graph("battery_amps"), "A")),
        (8, 4, 4, 3, xy("Charge power", victron_graph("battery_watts"), "W")),
        (0, 7, 4, 3, xy("Panel volts", victron_graph("pv_volts"), "V")),
        (4, 7, 4, 3, xy("Panel current", victron_graph("pv_amps"), "A")),
        (8, 7, 4, 3, xy("Panel power", victron_graph("pv_watts"), "W")),
        (0, 10, 12, 3, xy("Yield by day", VICTRON_DAILY, "kWh")),
    ]
    return dashboard(
        "blind1-victron", "Blind1: Victron",
        "The blind's solar charge controller.", cells)


# The garage's packs are every device on the rack, so a pack added there
# appears with no change here.

def garage_packs(pattern, scale=None):
    convert = ("" if scale is None else
               "    |> map(fn: (r) => ({r with _value: r._value * %s}))\n" % scale)
    return """\
from(bucket: "raw")
    |> range(start: v.timeRangeStart, stop: v.timeRangeStop)
    |> filter(fn: (r) => r._measurement == "garage" and r.location == "rack" and r._field =~ /%s/)
    |> group(columns: ["device", "_field"])
    |> aggregateWindow(every: v.windowPeriod, fn: mean, createEmpty: false)
%s""" % (pattern, convert)


#: The pack's protection states in words, in the order and with the codes
#: the driver reads them (monitoring/drivers/abc_bms.py, PROTECTION_STATES).
SOK_PROTECTION = [
    ("COV", "cell over volts"), ("FC", "full"), ("SOCC", "charge high"),
    ("OTC", "too warm to charge"), ("UTC", "too cold to charge"),
    ("BOTC", "board hot, charging"), ("CUV", "cell under volts"),
    ("FD", "empty"), ("SOCD", "charge low"),
    ("OTD", "too warm to discharge"), ("UTD", "too cold to discharge"),
    ("BOTD", "board hot, discharging"), ("SCD", "short circuit"),
    ("OCD", "over current"), ("UVP", "pack under volts"),
    ("OVP", "pack over volts"),
]

SOK_STATUS = """\
// One row per pack, its newest readings in the last thirty days. "read" is
// how long ago the pack last answered, so a pack that stopped stays listed
// with its last values. Alarms names each protection state the pack reports.

import "math"
import "strings"

alarms = (v) =>
    %s

ago = (t) => string(v: duration(v: (int(v: now()) - int(v: t)) / 1000000000 * 1000000000))

latest = from(bucket: "raw")
    |> range(start: -30d)
    |> filter(
        fn: (r) =>
            r._measurement == "garage" and r.location == "rack"
                and
                r._field =~ /^(state_of_charge_pct|pack_volts|pack_amps|cell_spread_volts|temp_[0-9]+|cycles|charge_mosfet_on|discharge_mosfet_on|heater_on|alarm_flags|commands_unanswered)$/,
    )
    |> last()

states = latest
    |> map(
        fn: (r) =>
            ({r with
                _value:
                    if r._field =~ /mosfet/ then
                        if r._value > 0.5 then "on" else "OFF"
                    else if r._field == "heater_on" then
                        if r._value > 0.5 then "on" else "off"
                    else if r._field == "alarm_flags" then
                        if r._value == 0.0 then "none" else alarms(v: int(v: r._value))
                    else if r._field == "cell_spread_volts" then
                        string(v: int(v: math.round(x: r._value * 1000.0)))
                    else if r._field == "pack_amps" or r._field == "pack_volts" or r._field =~ /^temp_/ then
                        string(v: math.round(x: r._value * 10.0) / 10.0)
                    else
                        string(v: int(v: math.round(x: r._value))),
            }),
    )

read = latest
    |> group(columns: ["device"])
    |> max(column: "_time")
    |> map(fn: (r) => ({r with _field: "read", _value: ago(t: r._time) + " ago"}))

union(tables: [states |> group(), read |> group()])
    |> keep(columns: ["device", "_field", "_value"])
    |> pivot(rowKey: ["device"], columnKey: ["_field"], valueColumn: "_value")
    |> sort(columns: ["device"])
"""

SOK_STATUS_COLUMNS = [
    ("device", "pack"), ("read", "read"), ("state_of_charge_pct", "charge %"),
    ("pack_volts", "volts"), ("pack_amps", "amps"),
    ("cell_spread_volts", "spread mV"), ("temp_1", "temp 1 F"),
    ("temp_2", "temp 2 F"), ("cycles", "cycles"),
    ("charge_mosfet_on", "charging"), ("discharge_mosfet_on", "discharging"),
    ("heater_on", "heater"), ("alarm_flags", "alarms"),
    ("commands_unanswered", "unanswered"),
]


def build_sok():
    status = SOK_STATUS % bit_names([words for _code, words in SOK_PROTECTION])
    cells = [
        (0, 0, 12, 3, status_table("Pack status", status, "device",
                                   SOK_STATUS_COLUMNS)),
        (0, 3, 6, 3, xy("State of charge", garage_packs("^state_of_charge_pct$"), "%")),
        (6, 3, 6, 3, xy("Current (positive is charging)", garage_packs("^pack_amps$"),
                        "A")),
        (0, 6, 6, 3, xy("Lowest and highest cell",
                        garage_packs("^cell_(min|max)_volts$"), "V")),
        (6, 6, 6, 3, xy("Cell spread", garage_packs("^cell_spread_volts$", "1000.0"),
                        "mV")),
        (0, 9, 6, 3, xy("Temperature", garage_packs("^temp_[0-9]+$"), "F")),
        (6, 9, 6, 3, xy("Pack volts", garage_packs("^pack_volts$"), "V")),
    ]
    return dashboard(
        "garage-sok", "Garage - SOK",
        "The garage's packs, from their own battery management systems.",
        cells)


def inverter_graph(pattern):
    return """\
from(bucket: "raw")
    |> range(start: v.timeRangeStart, stop: v.timeRangeStop)
    |> filter(fn: (r) => r._measurement == "garage" and r.device == "inverter" and r._field =~ /%s/)
    |> group(columns: ["_field"])
    |> aggregateWindow(every: v.windowPeriod, fn: mean, createEmpty: false)
""" % pattern


INVERTER_STATUS = """\
// The inverter's newest readings in the last thirty days. "read" is how long
// ago it last gave a reading, "answering" whether its newest poll got one,
// and "last error" the newest failure with how long ago it was. Words and
// numbers are read apart, since one stream holding both is a type conflict.

import "math"

ago = (t) => string(v: duration(v: (int(v: now()) - int(v: t)) / 1000000000 * 1000000000))

inverter = (fields) =>
    from(bucket: "raw")
        |> range(start: -30d)
        |> filter(fn: (r) => r._measurement == "garage" and r.device == "inverter" and r._field =~ fields)

latest = inverter(fields: /^(ok|ac_out_volts|ac_out_hz|load_watts|pv_watts|battery_volts|battery_watts)$/)
    |> last()

states = latest
    |> map(
        fn: (r) =>
            ({r with
                _value:
                    if r._field == "ok" then
                        if r._value > 0.5 then "yes" else "NO"
                    else if r._field == "ac_out_hz" or r._field == "battery_volts" then
                        string(v: math.round(x: r._value * 10.0) / 10.0)
                    else
                        string(v: int(v: math.round(x: r._value))),
            }),
    )

failed = inverter(fields: /^error_message$/)
    |> last()
    |> map(fn: (r) => ({r with _value: string(v: r._value) + " (" + ago(t: r._time) + " ago)"}))

read = latest
    |> filter(fn: (r) => r._field != "ok")
    |> group()
    |> max(column: "_time")
    |> map(fn: (r) => ({r with _field: "read", _value: ago(t: r._time) + " ago"}))

union(tables: [states |> group(), failed |> group(), read])
    |> keep(columns: ["_measurement", "_field", "_value"])
    |> pivot(rowKey: ["_measurement"], columnKey: ["_field"], valueColumn: "_value")
"""

INVERTER_STATUS_COLUMNS = [
    ("read", "read"), ("ok", "answering"), ("ac_out_volts", "AC volts"),
    ("ac_out_hz", "AC Hz"), ("load_watts", "load W"), ("pv_watts", "panels W"),
    ("battery_volts", "battery V"), ("battery_watts", "battery W"),
    ("error_message", "last error"),
]


def build_inverter():
    cells = [
        (0, 0, 12, 2, status_table("Inverter status", INVERTER_STATUS, "read",
                                   INVERTER_STATUS_COLUMNS)),
        (0, 2, 6, 3, xy("Power (battery positive is charging)",
                        inverter_graph("^(load_watts|pv_watts|battery_watts)$"), "W")),
        (6, 2, 6, 3, xy("Battery current (positive is charging)",
                        inverter_graph("^battery_amps$"), "A")),
        (0, 5, 4, 3, xy("AC out volts", inverter_graph("^ac_out_volts$"), "V")),
        (4, 5, 4, 3, xy("AC out frequency", inverter_graph("^ac_out_hz$"), "Hz")),
        (8, 5, 4, 3, xy("Battery volts", inverter_graph("^battery_volts$"), "V")),
        (0, 8, 6, 3, xy("Panel volts", inverter_graph("^pv_volts$"), "V")),
        (6, 8, 6, 3, xy("Panel current", inverter_graph("^pv_amps$"), "A")),
    ]
    return dashboard(
        "garage-charge-inverter", "Garage - Charge/Inverter",
        "The garage's inverter and charger.", cells)


def main(argv):
    outputs = [(OUT, build()), (HEALTH_OUT, build_health()),
               (CHINS_OUT, build_chins()), (VICTRON_OUT, build_victron()),
               (SOK_OUT, build_sok()), (INVERTER_OUT, build_inverter())]
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
