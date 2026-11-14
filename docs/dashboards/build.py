#!/usr/bin/env python3
"""Build the blind's energy dashboard from the daily energy query.

docs/queries/blind1-daily-energy.flux is the one place the daily figures
are worked out. The dashboard's daily cells are that query, started from the
dashboard's time range instead of a fixed lookback, so the two cannot
disagree. Run this after changing the query and commit what it writes:

    python3 docs/dashboards/build.py

With --check it writes nothing and exits 1 if the committed dashboard is out
of date.

The output imports through the InfluxDB web interface: Dashboards, Create
Dashboard, Import Dashboard.
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
// the two "loads from" series add up to the whole load, and all three to
// the charge controller's output. The shunt reports every minute and the
// controller every thirty seconds, so both are averaged over a common
// window of at least five minutes. Heater draw crosses the shunt as charge
// and shows as surplus to the bank.

w = if int(v: v.windowPeriod) < int(v: 5m) then 5m else v.windowPeriod

node = from(bucket: "raw")
    |> range(start: v.timeRangeStart, stop: v.timeRangeStop)
    |> filter(fn: (r) => r._measurement == "blind1" and r.node == "downstairs")

bank = node
    |> filter(
        fn: (r) => r.device == "shunt" and (r._field == "shunt_volts" or r._field == "shunt_amps"),
    )
    |> group()
    |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")
    |> filter(fn: (r) => exists r.shunt_volts and exists r.shunt_amps)
    |> map(fn: (r) => ({_time: r._time, _value: r.shunt_volts * r.shunt_amps}))
    |> aggregateWindow(every: w, fn: mean, createEmpty: false)

solar = node
    |> filter(fn: (r) => r.device == "mppt" and r._field == "battery_watts")
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
// A shunt step longer than two poll intervals spans a gap and is left out.

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
                r.elapsed <= 2 * int(v: poll) / int(v: 1s),
    )
    |> map(
        fn: (r) =>
            ({
                bin: date.truncate(t: r._time, unit: bin, location: timezone.utc),
                wh: if r.charging > 0.5 then 0.0 else r.cumulative_energy_wh,
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
    |> map(fn: (r) => ({bin: r.bin, one: 1.0}))

hours = dark
    |> sum(column: "one")
    |> map(fn: (r) => ({key: 1, hours: r.one * float(v: int(v: bin)) / float(v: int(v: 1h))}))

energy = join(tables: {s: shunt, d: dark}, on: ["bin"])
    |> sum(column: "wh")
    |> map(fn: (r) => ({key: 1, wh: r.wh}))

join(tables: {e: energy, h: hours}, on: ["key"])
    |> map(fn: (r) => ({_value: r.wh / r.hours}))
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


def query_entry(text):
    return {
        "text": text,
        "editMode": "advanced",
        "name": "",
        "builderConfig": {
            "buckets": [],
            "tags": [{"key": "_measurement", "values": [],
                      "aggregateFunctionType": "filter"}],
            "functions": [],
            "aggregateWindow": {"period": "auto", "fillValues": False},
        },
    }


PALETTE = [
    {"id": "a", "type": "scale", "hex": "#31C0F6", "name": "Nineteen Eighty Four", "value": 0},
    {"id": "b", "type": "scale", "hex": "#A500A5", "name": "Nineteen Eighty Four", "value": 0},
    {"id": "c", "type": "scale", "hex": "#FF7E27", "name": "Nineteen Eighty Four", "value": 0},
]


def axis(label="", bounds=("", "")):
    return {"bounds": list(bounds), "label": label, "prefix": "", "suffix": "",
            "base": "10", "scale": "linear"}


def xy(name, text, unit, position="overlaid", bounds=("", "")):
    return {
        "name": name,
        "properties": {
            "shape": "chronograf-v2",
            "type": "xy",
            "queries": [query_entry(text)],
            "axes": {"x": axis(), "y": axis(unit, bounds)},
            "colors": PALETTE,
            "geom": "line",
            "position": position,
            "xColumn": "_time",
            "yColumn": "_value",
            "hoverDimension": "auto",
            "legendColorizeRows": True,
            "legendOpacity": 1,
            "legendOrientationThreshold": 100000000,
            "timeFormat": "",
            "note": "",
            "showNoteWhenEmpty": False,
        },
    }


def single(name, text, suffix):
    return {
        "name": name,
        "properties": {
            "shape": "chronograf-v2",
            "type": "single-stat",
            "queries": [query_entry(text)],
            "colors": [{"id": "base", "type": "text", "hex": "#00C9FF",
                        "name": "laser", "value": 0}],
            "prefix": "",
            "tickPrefix": "",
            "suffix": suffix,
            "tickSuffix": "",
            "decimalPlaces": {"isEnforced": True, "digits": 0},
            "note": "",
            "showNoteWhenEmpty": False,
        },
    }


TABLE_COLUMNS = [
    "day", "bank_in_wh", "bank_out_wh", "bank_net_wh", "ah_net", "solar_wh",
    "load_wh", "direct_solar_wh", "night_load_wh", "night_hours",
    "night_avg_w", "solar_wh_check", "shunt_coverage_pct",
]


def table(name, text):
    return {
        "name": name,
        "properties": {
            "shape": "chronograf-v2",
            "type": "table",
            "queries": [query_entry(text)],
            "colors": [{"id": "base", "type": "text", "hex": "#00C9FF",
                        "name": "laser", "value": 0}],
            "tableOptions": {
                "verticalTimeAxis": True,
                "sortBy": {"internalName": "day", "displayName": "day",
                           "visible": True},
                "wrapping": "truncate",
                "fixFirstColumn": True,
            },
            "fieldOptions": [{"internalName": c, "displayName": c,
                              "visible": True} for c in TABLE_COLUMNS],
            "timeFormat": "YYYY-MM-DD",
            "decimalPlaces": {"isEnforced": True, "digits": 0},
            "note": "",
            "showNoteWhenEmpty": False,
        },
    }


def build():
    query = QUERY.read_text()
    # (x, y, w, h, view) on the dashboard's twelve-column grid.
    cells = [
        (0, 0, 9, 4, xy("Where the load's power comes from", SUPPLY, "W",
                        position="stacked")),
        (9, 0, 3, 6, single("Night draw, last 24 hours", NIGHT, " W")),
        (0, 4, 9, 2, xy("Chins state of charge", SOC, "%", bounds=("0", "100"))),
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
    included = []
    refs = []
    for number, (x, y, w, h, view) in enumerate(cells, start=1):
        ident = str(number)
        refs.append({"type": "cell", "id": ident})
        included.append({
            "id": ident, "type": "cell",
            "attributes": {"x": x, "y": y, "w": w, "h": h},
            "relationships": {"view": {"data": {"type": "view", "id": ident}}},
        })
        included.append({"id": ident, "type": "view", "attributes": view})
    return {
        "meta": {"version": "1", "type": "dashboard",
                 "name": "Blind energy-Template", "description": ""},
        "content": {
            "data": {
                "type": "dashboard",
                "attributes": {
                    "name": "Blind energy",
                    "description": "The blind's battery bank and solar, "
                                   "from the shunt and the charge controller.",
                },
                "relationships": {
                    "label": {"data": []},
                    "cell": {"data": refs},
                    "variable": {"data": []},
                },
            },
            "included": included,
        },
        "labels": [],
    }


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
