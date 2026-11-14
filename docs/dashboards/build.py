#!/usr/bin/env python3
"""Build the blind's energy dashboard from the daily energy query.

docs/queries/blind1-daily-energy.flux is the one place the daily figures
are worked out. The dashboard's daily cells are that query, started from the
dashboard's time range instead of a fixed lookback, so the two cannot
disagree. Run this after changing the query and commit what it writes:

    python3 docs/dashboards/build.py

With --check it writes nothing and exits 1 if the committed dashboard is out
of date.

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

FIXED_START = "start = date.truncate(t: lookback, unit: 1d)\n"
PICKED_START = "start = date.truncate(t: v.timeRangeStart, unit: 1d)\n"
FINAL = "\nunion(\n"
NAMED_FINAL = "\ndaily = union(\n"

SUPPLY = """\
// Where the loads' power comes from, and where the surplus goes. Stacked,
// the two "loads from" series add up to the whole load. The top edge is the
// load or the charge controller's output, whichever is larger. The shunt
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

bank = node(device: "shunt")
    |> filter(fn: (r) => r._field == "shunt_volts" or r._field == "shunt_amps")
    |> group()
    |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")
    |> filter(fn: (r) => exists r.shunt_volts and exists r.shunt_amps)
    |> map(fn: (r) => ({_time: r._time, _value: r.shunt_volts * r.shunt_amps}))
    |> aggregateWindow(every: w, fn: mean, createEmpty: false)

solar = node(device: "mppt")
    |> filter(fn: (r) => r._field == "battery_watts")
    |> group()
    |> keep(columns: ["_time", "_value"])
    |> aggregateWindow(every: w, fn: mean, createEmpty: false)

both = join(tables: {b: bank, s: solar}, on: ["_time"])

from_bank = both
    |> map(
        fn: (r) =>
            ({_time: r._time, _field: "loads from battery",
                _value: if r._value_b < 0.0 then 0.0 - r._value_b else 0.0,
            }),
    )

from_solar = both
    |> map(
        fn: (r) => {
            into = if r._value_b > 0.0 then r._value_b else 0.0
            direct = r._value_s - into

            return {_time: r._time, _field: "loads from solar",
                _value: if direct > 0.0 then direct else 0.0,
            }
        },
    )

to_bank = both
    |> map(
        fn: (r) =>
            ({_time: r._time, _field: "surplus to bank",
                _value: if r._value_b > 0.0 then r._value_b else 0.0,
            }),
    )

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
    |> elapsed(unit: 1s)
    |> difference(columns: ["cumulative_energy_wh"], nonNegative: true)
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
    charts = []
    for x, y, w, h, chart in cells:
        placed = {"name": chart["name"], "xPos": x, "yPos": y,
                  "width": w, "height": h}
        placed.update((k, v) for k, v in chart.items() if k != "name")
        charts.append(placed)
    return [{
        "apiVersion": "influxdata.com/v2alpha1",
        "kind": "Dashboard",
        "metadata": {"name": "blind-energy"},
        "spec": {
            "name": "Blind energy",
            "description": "The blind's battery bank and solar, from the "
                           "shunt and the charge controller.",
            "charts": charts,
        },
    }]


def main(argv):
    text = json.dumps(build(), indent=2) + "\n"
    if argv[1:] == ["--check"]:
        if not OUT.exists() or OUT.read_text() != text:
            print("%s is out of date; run %s" % (OUT, __file__), file=sys.stderr)
            return 1
        return 0
    if argv[1:]:
        print("usage: %s [--check]" % argv[0], file=sys.stderr)
        return 2
    OUT.write_text(text)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
