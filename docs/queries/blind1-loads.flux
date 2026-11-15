import "strings"

// What each load draws, as the controller last measured it. The controller
// measures a load as the step in the bank's current when it switches the
// load, keeps the latest figure on its card, and publishes it on every
// pass, so the last week always holds every figure it has. See
// monitoring/control/meter.py.
//
// A load's figures are fields of its own point: draw_<condition>_amps and
// draw_<condition>_at, the epoch it was measured. The base load is
// base_amps and base_at on the controller's own point.
from(bucket: "raw")
    |> range(start: -7d)
    |> filter(fn: (r) => r._measurement == "blind1" and r.node == "downstairs" and r.device == "control")
    |> filter(fn: (r) => r._field =~ /^(draw(_[a-z]+)?|base)_(amps|at)$/)
    |> last()
    |> map(
        fn: (r) => {
            kind = if strings.hasSuffix(v: r._field, suffix: "_amps") then "amps" else "at"
            stem = strings.trimSuffix(v: r._field, suffix: "_" + kind)
            load = if exists r.load then r.load else "base"
            condition = if stem == "draw" or stem == "base" then "" else strings.trimPrefix(v: stem, prefix: "draw_")

            return {row: load + " " + condition, kind: kind, _value: float(v: r._value)}
        },
    )
    |> group()
    |> pivot(rowKey: ["row"], columnKey: ["kind"], valueColumn: "_value")
    |> filter(fn: (r) => exists r.amps and exists r.at)
    |> map(
        fn: (r) => ({
            load: if r.row == "cameras_far night" then "Far cameras at night"
                else if r.row == "cameras_far day" then "Far cameras by day"
                else if r.row == "cameras_near night" then "Near cameras at night"
                else if r.row == "cameras_near day" then "Near cameras by day"
                else if r.row == "irrigation wet" then "Pump moving water, with inverter"
                else if r.row == "irrigation dry" then "Pump running dry, with inverter"
                else if r.row == "irrigation high" then "Pump above its band, with inverter"
                else if r.row == "cpu_fan " then "Pi fan"
                else if r.row == "base " then "Base load: recorder, Pi, network, radios"
                else strings.trimSpace(v: r.row),
            amps: r.amps,
            watts_at_13v2: r.amps * 13.2,
            measured: time(v: int(v: r.at * 1000000000.0)),
        }),
    )
    |> sort(columns: ["load"])
