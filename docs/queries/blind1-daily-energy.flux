// The blind's daily energy, one row per local day, in watt-hours.
//
// Two levels. The bank level is what crosses the shunt, which sits where the
// battery bank joins the rest of the system. The system level adds the
// charge controller: solar that feeds a load directly never crosses the
// shunt, so the loads' total is the controller's production less what the
// bank kept.
//
//   bank_in_wh        energy into the bank through the shunt
//   bank_out_wh       energy out of the bank through the shunt
//   bank_net_wh       bank_in_wh less bank_out_wh
//   ah_net            change in the shunt's remaining capacity
//   solar_wh          the controller's production, from its lifetime yield
//   load_wh           solar_wh less bank_net_wh: everything the loads used
//   direct_solar_wh   solar_wh less bank_in_wh: solar the loads used directly
//   night_load_wh     bank_out_wh while the controller produced nothing
//   night_hours       how long the controller produced nothing
//   night_avg_w       night_load_wh over night_hours
//   solar_wh_check    the controller's output power, integrated
//   shunt_coverage_pct  shunt readings received, of one a poll interval
//                       in the day so far
//
// The shunt's cumulative_energy_wh counts energy in either direction, so it
// only ever rises. Each rise between two readings is filed by the shunt's
// charging flag on the later reading. A rise that spans a change of
// direction is filed whole under one side, so the split is good to about one
// poll interval's energy at each change; their sum is exact. A gap in the
// readings is kept whole and filed by the reading that ends it, in that
// reading's direction and on that reading's day. night_load_wh leaves out
// any step longer than two poll intervals, which could hold daylight.
//
// A counter that falls has been reset, and that one interval is dropped.
// Each day reads one hour before its midnight, so its first interval has a
// reading to start from.
//
// Self-heating packs draw their heaters from the charge current through the
// shunt. That energy is in bank_in_wh and bank_net_wh without being stored.
// It is not in load_wh, which is therefore the loads alone plus the wiring
// loss between the two meters.
//
// The night is wherever every controller reading in a five-minute bin shows
// no panel power. An overcast day still produces, so it is not night. The
// bins are cut in UTC: on the day the clocks fall back, local bins would
// fold the repeated hour onto the first one. Chicago's offsets are whole
// hours, so UTC bins line up with local ones.
//
// ah_net flattens wherever the shunt clamps at full or empty.
//
// solar_wh_check holds each output-power snapshot, one every thirty
// seconds, for the interval it ends, at most five minutes. It misses short
// changes. A large gap from solar_wh means missing controller readings, not
// a better figure. A controller outage across midnight puts its yield in
// solar_wh on the later day.

import "date"
import "timezone"

option location = timezone.location(name: "America/Chicago")

// How far back. Truncated to local midnight so the first day is whole. A day
// short of thirty, so the first day stays inside a bucket kept for thirty.
lookback = -29d

// The shunt's poll interval, from the node's slow_poll_interval. A step
// longer than two of these spans a gap in the readings.
poll = 60s

start = date.truncate(t: lookback, unit: 1d)
lead = date.sub(d: 1h, from: start)
bin = 5m

node = (device) => from(bucket: "raw")
    |> range(start: lead)
    |> filter(fn: (r) => r._measurement == "blind1" and r.node == "downstairs" and r.device == device)

// One row per field and day, ready to be put side by side.
stat = (tables=<-, name, col) =>
    tables
        |> keep(columns: ["day", col])
        |> rename(fn: (column) => if column == col then "_value" else column)
        |> group(columns: ["day"])
        |> sum()
        |> map(fn: (r) => ({day: r.day, _field: name, _value: float(v: r._value)}))
        |> group()

shunt = node(device: "shunt")
    |> filter(
        fn: (r) =>
            (r._field == "cumulative_energy_wh" or r._field == "charging"
                or
                r._field == "remaining_capacity_ah"),
    )
    |> group()
    |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")
    |> filter(fn: (r) => exists r.cumulative_energy_wh and exists r.charging)
    |> sort(columns: ["_time"])
    |> elapsed(unit: 1s)

steps = shunt
    |> difference(columns: ["cumulative_energy_wh"], nonNegative: true)
    |> filter(fn: (r) => exists r.cumulative_energy_wh and r._time >= start)
    |> map(
        fn: (r) =>
            ({
                _time: r._time,
                gap: r.elapsed > 2 * int(v: poll) / int(v: 1s),
                day: date.truncate(t: r._time, unit: 1d),
                bin: date.truncate(t: r._time, unit: bin, location: timezone.utc),
                wh_in: if r.charging > 0.5 then r.cumulative_energy_wh else 0.0,
                wh_out: if r.charging > 0.5 then 0.0 else r.cumulative_energy_wh,
            }),
    )

bank_in = steps |> stat(name: "bank_in_wh", col: "wh_in")
bank_out = steps |> stat(name: "bank_out_wh", col: "wh_out")

coverage = shunt
    |> filter(fn: (r) => r._time >= start)
    |> map(
        fn: (r) => {
            day = date.truncate(t: r._time, unit: 1d)
            next = date.add(d: 1d, to: day)
            end = if next > now() then now() else next
            expected = float(v: int(v: end) - int(v: day)) / float(v: int(v: poll))

            return {day: day, pct: 100.0 / expected}
        },
    )
    |> stat(name: "shunt_coverage_pct", col: "pct")

ah_net = shunt
    |> filter(fn: (r) => exists r.remaining_capacity_ah)
    |> difference(columns: ["remaining_capacity_ah"])
    |> filter(fn: (r) => exists r.remaining_capacity_ah and r._time >= start)
    |> map(fn: (r) => ({day: date.truncate(t: r._time, unit: 1d), ah: r.remaining_capacity_ah}))
    |> stat(name: "ah_net", col: "ah")

mppt = (field) => node(device: "mppt") |> filter(fn: (r) => r._field == field)

solar = mppt(field: "yield_total_kwh")
    |> group()
    |> sort(columns: ["_time"])
    |> difference(nonNegative: true)
    |> filter(fn: (r) => exists r._value and r._time >= start)
    |> map(fn: (r) => ({day: date.truncate(t: r._time, unit: 1d), wh: r._value * 1000.0}))
    |> stat(name: "solar_wh", col: "wh")

solar_check = mppt(field: "battery_watts")
    |> group()
    |> sort(columns: ["_time"])
    |> elapsed(unit: 1s)
    |> filter(fn: (r) => r._time >= start)
    |> map(
        fn: (r) =>
            ({
                day: date.truncate(t: r._time, unit: 1d),
                wh: r._value * float(v: if r.elapsed > 300 then 300 else r.elapsed) / 3600.0,
            }),
    )
    |> stat(name: "solar_wh_check", col: "wh")

dark = mppt(field: "pv_watts")
    |> filter(fn: (r) => r._time >= start)
    |> group()
    |> map(fn: (r) => ({bin: date.truncate(t: r._time, unit: bin, location: timezone.utc), pv: r._value}))
    |> group(columns: ["bin"])
    |> max(column: "pv")
    |> group()
    |> filter(fn: (r) => r.pv <= 0.0)
    |> map(fn: (r) => ({bin: r.bin, day: date.truncate(t: r.bin, unit: 1d), hours: float(v: int(v: bin)) / float(v: int(v: 1h))}))

night_hours = dark |> stat(name: "night_hours", col: "hours")

night_load = join(
    tables: {
        s: steps |> filter(fn: (r) => not r.gap) |> group() |> drop(columns: ["_time", "day"]),
        d: dark,
    },
    on: ["bin"],
)
    |> stat(name: "night_load_wh", col: "wh_out")

union(
    tables: [
        bank_in,
        bank_out,
        coverage,
        ah_net,
        solar,
        solar_check,
        night_hours,
        night_load,
    ],
)
    |> group()
    |> pivot(rowKey: ["day"], columnKey: ["_field"], valueColumn: "_value")
    |> map(
        fn: (r) =>
            ({r with
                bank_net_wh: r.bank_in_wh - r.bank_out_wh,
                load_wh: r.solar_wh - (r.bank_in_wh - r.bank_out_wh),
                direct_solar_wh: r.solar_wh - r.bank_in_wh,
                night_avg_w: r.night_load_wh / r.night_hours,
            }),
    )
    |> keep(
        columns: [
            "day",
            "bank_in_wh",
            "bank_out_wh",
            "bank_net_wh",
            "ah_net",
            "solar_wh",
            "load_wh",
            "direct_solar_wh",
            "night_load_wh",
            "night_hours",
            "night_avg_w",
            "solar_wh_check",
            "shunt_coverage_pct",
        ],
    )
    |> sort(columns: ["day"])
