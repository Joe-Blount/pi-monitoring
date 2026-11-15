// What each of the blind's loads draws, from the last time it was measured.
//
// A load is measured when the controller switches it: the step in the
// bank's current across the switch is the load's draw. Each row is the
// latest such step for a load, with when it was taken, how many were taken
// in the last thirty days, and their average.
//
// The recorder's branch is wired around its relay, so it is never switched.
// Its row is the bank's draw while every relay load has been off for five
// minutes, with no charge coming in. That holds the recorder and everything
// that cannot be shed: the Pi, the network gear, the charge controller and
// the battery radios. The two are not measured apart.
//
// Watts are at a nominal 13.2 V.

import "array"
import "date"
import "join"
import "strings"
import "timezone"

option location = timezone.location(name: "America/Chicago")

start = -30d

node = (device, fields) => from(bucket: "raw")
    |> range(start: start)
    |> filter(fn: (r) => r._measurement == "blind1" and r.node == "downstairs" and r.device == device and r._field =~ fields)

// The controller's relay loads. The recorder's relay is listed but its
// branch is wired around it, so it is never measured.
relays = node(device: "control", fields: /^powered$/)
    |> filter(fn: (r) => r.load =~ /^(cameras_far|cameras_near|cpu_fan|irrigation|irrigation[.]inverter)$/)

// Switches in one publish are one event. The pump and its inverter start
// and stop together, and that is the pump; any other pair is two loads at
// once and is measured as neither. Each event knows how long since the one
// before it and until the one after, which bounds its windows.
events = relays
    |> group(columns: ["load"])
    |> sort(columns: ["_time"])
    |> elapsed(unit: 1s)
    |> difference()
    |> filter(fn: (r) => r._value != 0.0)
    // A change across a gap in the controller's publishes, such as a
    // restart, happened at some time inside the gap. It still bounds its
    // neighbours' windows, but it is not measured.
    // Its window opens at the last publish before the gap.
    |> map(
        fn: (r) => {
            gap = r.elapsed > 45

            return {r with load: if gap then "gap" else r.load,
                opened: int(v: r._time) - (if gap then r.elapsed * int(v: 1s) else 0)}
        },
    )
    |> group(columns: ["_time"])
    |> reduce(
        identity: {n: 0, names: "", dir: 0.0, pump_dir: 0.0, opened: 0},
        fn: (r, accumulator) => ({
            n: accumulator.n + 1,
            names: accumulator.names + r.load + " ",
            dir: r._value,
            pump_dir: if r.load == "irrigation" then r._value else accumulator.pump_dir,
            opened: if accumulator.n == 0 or r.opened < accumulator.opened then r.opened else accumulator.opened,
        }),
    )
    |> group()
    |> sort(columns: ["_time"])
    |> map(
        fn: (r) => {
            pump = r.n == 2 and strings.containsStr(v: r.names, substr: "irrigation ")
                and strings.containsStr(v: r.names, substr: "irrigation.inverter ")
            load = if r.n == 1 then strings.trimSpace(v: r.names) else if pump then "irrigation" else "several"

            return {_time: r._time, load: load, dir: if pump then r.pump_dir else r.dir,
                t: int(v: r._time), opened: r.opened}
        },
    )
    |> duplicate(column: "t", as: "t_prev")
    |> difference(columns: ["t_prev"], keepFirst: true)
    |> sort(columns: ["_time"], desc: true)
    |> duplicate(column: "opened", as: "t_next")
    |> difference(columns: ["t_next"], keepFirst: true)
    |> sort(columns: ["_time"])
    |> map(
        fn: (r) => ({
            ev: r._time,
            load: r.load,
            dir: r.dir,
            since: if exists r.t_prev then r.t_prev else int(v: 1h),
            // From this event to the next one's opening.
            until: if exists r.t_next then r.opened - r.t - r.t_next else int(v: 1h),
        }),
    )
    |> filter(fn: (r) => r.load != "several" and r.load != "gap")

// One event that meets no reading, so the joins below never see an empty
// side when the controller switched nothing in the range.
none = array.from(rows: [{ev: 1970-01-01T00:00:00Z, load: "none", dir: 0.0, since: 0, until: 0}])

// Each event is offered every thirty-second slot from three minutes before
// it to five minutes after, and a reading meets the events whose slots hold
// it. The events are few, so they are the side that is copied.
slots = (d) => union(tables: [events, none])
    |> map(fn: (r) => ({r with key: date.truncate(t: date.add(d: d, to: r.ev), unit: 30s)}))

offered = union(
    tables: [
        slots(d: -210s), slots(d: -180s), slots(d: -150s), slots(d: -120s),
        slots(d: -90s), slots(d: -60s), slots(d: -30s), slots(d: 0s),
        slots(d: 30s), slots(d: 60s), slots(d: 90s), slots(d: 120s),
        slots(d: 150s), slots(d: 180s), slots(d: 210s), slots(d: 240s),
        slots(d: 270s), slots(d: 300s),
    ],
)

readings = (device, field) => node(device: device, fields: field)
    |> group()
    |> map(fn: (r) => ({_time: r._time, value: r._value, key: date.truncate(t: r._time, unit: 30s)}))

// The controller publishes a switch up to thirty seconds after it makes
// it, so a reading in those thirty seconds before a publish may already
// hold that switch's step, and is used on neither side of it. A camera's
// own start draws more than it runs on, so its after window opens two
// minutes on, and its step is the mean of the readings in it. The pump is
// measured on its first reading after the switch is published, close to
// where the controller checks it. Before an event, the last reading stands.
settle = (load) => if load == "irrigation" then 0 else int(v: 2m)

// The thirty seconds kept clear before the next publish. A run the check
// stops is stopped by the very reading that measures it, so that reading
// always comes before the stop.
margin = (load) => if load == "irrigation" then 0 else int(v: 30s)

side = (l, t) => {
    age = int(v: t) - int(v: l.ev)

    return if age < -int(v: 30s) and -age <= int(v: 3m) and -age < l.since then "before"
        else if age > settle(load: l.load) and age <= int(v: 270s) and age < l.until - margin(load: l.load) then "after"
        else "none"
}

met = (device, field) => join.inner(
    left: offered,
    right: readings(device: device, field: field),
    on: (l, r) => l.key == r.key,
    as: (l, r) => ({ev: l.ev, load: l.load, dir: l.dir, side: side(l: l, t: r._time), _time: r._time, value: r.value}),
)
    |> filter(fn: (r) => r.side != "none")

amps = met(device: "shunt", field: /^shunt_amps$/)
    |> group(columns: ["ev", "load", "dir", "side"])
    |> sort(columns: ["_time"])

// Any charge while the step is measured, from the panels or a generator,
// would hide part of it, so such an event is not measured. The charge is
// looked for from the reading the step starts from to the last one it
// ends on: in time order, a charge controller reading counts once that
// first reading has passed.
steps = union(
    tables: [
        amps |> filter(fn: (r) => r.side == "before") |> last(column: "_time"),
        amps |> filter(fn: (r) => r.side == "after" and r.load != "irrigation"),
        amps |> filter(fn: (r) => r.side == "after" and r.load == "irrigation") |> first(column: "_time"),
        met(device: "mppt", field: /^battery_amps$/)
            |> map(fn: (r) => ({ev: r.ev, load: r.load, dir: r.dir, side: "charge", _time: r._time, value: r.value})),
    ],
)
    |> group(columns: ["ev", "load", "dir"])
    |> sort(columns: ["_time"])
    |> reduce(
        identity: {started: false, before: 0.0, after: 0.0, n: 0.0, last: 0, charging: false},
        fn: (r, accumulator) => {
            started = accumulator.started or r.side == "before"
            measured = r.side == "after" and started

            return {
                started: started,
                before: if r.side == "before" then r.value else accumulator.before,
                after: if measured then accumulator.after + r.value else accumulator.after,
                n: if measured then accumulator.n + 1.0 else accumulator.n,
                last: if measured then int(v: r._time) else accumulator.last,
                charging: accumulator.charging or (r.side != "charge" and r.value > 0.0)
                    or (r.side == "charge" and started and r.value > 0.0
                        and (accumulator.last == 0 or int(v: r._time) <= accumulator.last)),
            }
        },
    )
    |> group()
    |> filter(fn: (r) => r.started and r.n > 0.0 and not r.charging)
    |> map(fn: (r) => ({ev: r.ev, load: r.load, dir: r.dir, before: r.before, after: r.after / r.n}))

steps_labelled = steps
    |> map(
        fn: (r) => {
            amps = (r.before - r.after) * r.dir
            // Hours that are dark, or light, all year here. A camera's
            // infrared lights draw at night, so the two are kept apart, and
            // a switch in the hours between is left out.
            hour = date.hour(t: r.ev)
            when = if hour < 6 or hour >= 21 then "night" else if hour >= 9 and hour < 16 then "day" else "twilight"

            // The pump's classes are the band its check uses in the node
            // file: 30 to 60 A is a pump moving water. Like the check, the
            // step holds the inverter's idle draw, since the last reading
            // before a start almost always comes before the inverter's. A
            // first reading can land in the pump's starting surge, so a
            // high one may be that rather than a stall.
            label = if r.load == "cameras_far" then "Far cameras, " + when
                else if r.load == "cameras_near" then "Near cameras, " + when
                else if r.load == "irrigation" and amps < 30.0 then "Pump, dry"
                else if r.load == "irrigation" and amps > 60.0 then "Pump, stalled or still starting"
                else if r.load == "irrigation" then "Pump, wet (inverter included)"
                else if r.load == "irrigation.inverter" then "Inverter, idle"
                else if r.load == "cpu_fan" then "Pi fan"
                else r.load

            return {label: label, _time: r.ev, amps: amps}
        },
    )
    |> filter(fn: (r) => not strings.hasSuffix(v: r.label, suffix: "twilight"))

// The draw with every relay load off for a whole five minutes, no charge
// coming in and the bank discharging: the recorder, whose branch the relay
// does not switch, and everything that cannot be shed.
window = (tables=<-, fn) => tables
    |> aggregateWindow(every: 5m, fn: fn, createEmpty: false, timeSrc: "_start")
    |> group()

busy = relays
    |> group(columns: ["load"])
    |> window(fn: max)
    |> group(columns: ["_time"])
    |> sum()
    |> group()
    |> map(fn: (r) => ({_time: r._time, kind: "busy", value: r._value}))

// A load the controller switches off is published up to thirty seconds
// later, so the window before must be quiet too.
rest = union(
    tables: [
        busy,
        busy |> timeShift(duration: 5m) |> map(fn: (r) => ({_time: r._time, kind: "busy_before", value: r.value})),
        node(device: "shunt", fields: /^shunt_amps$/) |> window(fn: mean)
            |> map(fn: (r) => ({_time: r._time, kind: "amps", value: r._value})),
        node(device: "mppt", fields: /^battery_amps$/) |> window(fn: max)
            |> map(fn: (r) => ({_time: r._time, kind: "solar", value: r._value})),
    ],
)
    |> pivot(rowKey: ["_time"], columnKey: ["kind"], valueColumn: "value")
    |> filter(fn: (r) => exists r.busy and exists r.busy_before and exists r.amps and exists r.solar)
    |> filter(fn: (r) => r.busy == 0.0 and r.busy_before == 0.0 and r.solar <= 0.0 and r.amps < 0.0)
    |> sort(columns: ["_time"])
    |> map(fn: (r) => ({label: "Recorder and base load", _time: r._time, amps: -r.amps}))

// In time order, so each load ends on its latest measurement.
union(tables: [steps_labelled, rest])
    |> group(columns: ["label"])
    |> sort(columns: ["_time"])
    |> reduce(
        identity: {n: 0, total: 0.0, amps: 0.0, measured: 1970-01-01T00:00:00Z},
        fn: (r, accumulator) => ({n: accumulator.n + 1, total: accumulator.total + r.amps, amps: r.amps, measured: r._time}),
    )
    |> group()
    |> map(
        fn: (r) => ({
            load: r.label,
            amps: r.amps,
            watts_at_13v2: r.amps * 13.2,
            measured: r.measured,
            times: r.n,
            average_amps: r.total / float(v: r.n),
        }),
    )
    |> sort(columns: ["load"])
