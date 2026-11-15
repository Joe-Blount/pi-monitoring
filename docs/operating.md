# Operating the system

For the person standing at a Pi who needs to change something and cannot ask
anyone first. Each heading is a question. Under it is the exact command.

`<file>` means the node file for the machine you are standing at. On an
installed machine that is always:

```
/etc/monitoring/node.yaml
```

`<load>` means a load's name from that machine's `control:` block, such as
`cameras_near`. Find the names for this machine with:

```
collect --node <file> --list
```

It also prints `control: enabled` or `control: disabled`. The commands below
that touch a relay only do anything on a machine where control is enabled.

Every `control` command below is shown with `sudo`, and needs it. The
overrides, the shedding switch and the readings the controller decides from
all live where only the service user can read and write them, so the same
command without `sudo` stops with a permission error rather than doing
nothing silently. `collect` needs no `sudo`.

---

## Two things that look like a fault and are not

**The system sits at its shallowest shed tier for weeks in winter, and does
nothing else.** A tier only releases once the battery clears that tier's own
recovery point, set well above where the tier first latched, so the relay
does not chatter at the boundary. A bank sitting in the forties percent all
winter never reaches a release point set at sixty-five percent, so the
system stays latched at that one tier, pass after pass, correctly. That is
not stuck. It is the design holding the same correct answer for as long as
the weather does.

**A DHT temperature sensor reads, then goes quiet, then reads again, with no
wiring change.** The kernel driver refuses a frame whose decimal digits it
cannot classify, and a sensor reporting tenths of a degree only passes that
check while the temperature happens to sit on a whole degree. Retrying gets
past it some of the time and not others, for no reason you can see at the
sensor. The full explanation, and the kernel version that fixes it, is in
[docs/bringup.md](bringup.md), under Troubleshooting.

---

## Force a load from a phone

A node that runs load control serves a page on port 8088. Open it from a
phone on the tailnet, or from a device on the site Wi-Fi:

```
http://<the node's tailnet address>:8088/
```

The page shows each load, whether it is powered and why, any force in effect,
and the readings the loads decide on. Each load has buttons to force it on or
off for 5 minutes up to 12 hours, or until a local time. Every force ends on
its own. The page also turns shedding on and off.

The page changes the same file as the commands below, so the two always
agree. It answers only the tailnet and the site network. If the page warns that
the controller has not published, a force has no effect until the controller
runs again.

## Run the irrigation, or the inverter alone

The irrigation load has two rows on the page. **irrigation** runs the pump:
the controller turns on the inverter, waits for it to settle, then starts the
pump. **irrigation.inverter** turns on the inverter alone, for AC power, and
the pump stays off. From the command line:

```
sudo control override irrigation on --for 20m
sudo control override irrigation.inverter on --for 4h
```

A run stops at the first of these, whatever was asked:

- the time asked for, or the 45 minute run limit
- charge at or below 30 percent, or the battery at or below 12.6 V
- a pump that draws less than 30 A or more than 60 A, checked on the first
  shunt reading 15 s after it starts: dry, lost its prime, or stalled
- no fresh shunt reading
- 160 Ah used in the day

A pump that fails its check counts as a failed start. After three in one
day, irrigation locks out until the next day. Clear the lockout from the page
or with:

```
sudo control clear-lockout irrigation
```

The inverter alone stops at the same battery limits, and it comes back on
only when the charge is above 40 percent and the battery above 13.0 V.

## Force the cameras on right now

Each camera group is its own load: `cameras_near`, `cameras_far`, and the
recorder, `nvr`. Force each one you want:

```
sudo control override cameras_near on --for 8h
```

It runs for the time you gave and then returns to normal control on its own.
The controller then removes the expired force from the switches file, so it
no longer shows on the override page. It does that only while it trusts the
clock. Clear it sooner:

```
sudo control override clear cameras_near
```

## Force the cameras off

```
sudo control override cameras_far off --until "YYYY-MM-DD HH:MM"
```

Always give an end time. An override with no end time is treated as already
expired, so it does nothing at all: write one even if it is generous, rather
than none.

## Stop the controller shedding anything, without stopping the controller

```
sudo control shedding off
```

Every load currently shed by a tier is handed back immediately. A CPU fan is
never shed by a tier, so this switch does not change it: it still follows its
temperature. Check the state with:

```
sudo control shedding status
```

Turn it back on the same way:

```
sudo control shedding on
```

This switch cannot save a battery from itself. A cell under its own voltage
floor still sheds regardless of this switch, an override, the schedule, or
any minimum run time. That one rule has nothing above it.

It also does not shorten the wait after a floor shed. Once the cell has
recovered, the load comes back on the minimum off time like any other, and
neither this switch nor an override hurries it. The reason the controller
reports says so, with the seconds left. A cell weak enough to sag under this
load and recover without it would otherwise cycle the recorder as fast as it
crosses its own bounds, and this is the switch most likely to be left off for
weeks.

## Stop the controller entirely

```
sudo systemctl stop monitoring-control
sudo systemctl disable monitoring-control
```

Stop alone lasts until the next reboot, because the service is enabled.
Disable as well if you want it to stay down.

If you started it by hand rather than through the service:

```
pkill -f "control run"
```

Either way, every load drops to whatever its wiring does with no signal at
all, which is the fail-safe state, not a guess: see the table below for what
that is per load. Cameras keep running. Irrigation and heating stay off.

## See what the controller would do, without letting it touch a relay

```
sudo control dry-run --node <file>
```

It reads the same inputs and reaches the same decision as a real pass, and
prints it. No pin moves.

## Turn load control on for this machine

Two switches, both in the node file, and both have to move on the same
visit.

```
control:
  enabled: true
```

and the device that reports what the controller decided:

```
  - name: control
    driver: control
    enabled: true
```

Then:

```
sudo systemctl enable --now monitoring-control
```

Each switch alone is wrong in its own direction. The controller on with the
reporting device off is a machine that sheds loads and tells nobody: the
shed level and the reason code reach no dashboard, and a controller that has
died looks exactly like one with nothing to do. The reporting device on with
the controller off is a collector that fails every interval saying the
controller has published nothing, which is true, and is noise.

The suite refuses a node file where the two disagree, so a mistake here
shows up before the machine does.

## Why are the cameras off, and which number turned them off

Read the controller's own last decision straight from the file it writes,
which needs nothing else running:

```
sudo cat /run/monitoring/control.json
```

Inside, under `loads` and then the load's name, look for three fields:

```
reason          text: which rule fired, and the number it compared
reason_code     the same rule, as a number (table below)
decided_on      the bare number the rule compared, by itself
```

`reason` reads something like `state_of_charge_pct=9.4 below 12%`. That
number is the one that turned the load off. `shed_level` says how many tiers
are currently latched; `0` means none and the load should be running unless
something else is holding it off.

Do not read `level` as whether the cameras have power. It is whether the
relay's coil is energized, not the load's state, and for a normally-closed
branch the two are opposite: `level=0.0` next to `powered=1.0` means the relay
is de-energized and the cameras are running, which is correct. Use `powered`
for whether the load has power. Nor is `level` the voltage on the pin: on a
board that triggers low, an energized coil is a pin driven low.

In the database the same fields carry the same names, on a point tagged
`load=cameras_near`. See the table of published fields below.

## Read one sensor by hand

```
collect <device> --node <file>
```

For example:

```
collect host --node <file>
```

To see the device's own unparsed output instead of the parsed reading:

```
collect host --node <file> --raw
```

## Tell whether data is reaching the database

```
collect --node <file> --poll
```

This only prints; nothing it does writes to the database. If lines print
here with no error, the collector side is fine and the question is telegraf:

```
systemctl is-active telegraf
journalctl -u telegraf -f
```

Watch it for a minute. A failed write is always logged, never silently
dropped, so a `401` or `403` shows up here rather than as a gap nobody can
explain. Full walk-through: [docs/bringup.md](bringup.md), section 5.

If the controller is enabled on this machine and nothing it decides ever
changes, check that its reading cache is writable:

```
ls -ld /run/monitoring/readings
```

If that directory is owned by root because someone ran `collect` by hand
with `sudo`, the service user can no longer write to it, and the controller
is deciding from nothing.

## Dashboards

[docs/dashboards/cloud](dashboards/cloud) holds the dashboards made by hand
in InfluxDB Cloud, as Cloud exports them. They are a record to compare and
edit against; Cloud is where they are changed until one moves to the
builder.

Cloud's export leaves out which column colours a Mosaic cell. After
importing [load-controller.json](dashboards/cloud/load-controller.json), the
"Load power" cell says its data cannot be shown until that is set: edit the
cell and pick _value as the fill column. Every cell reads the dashboard
variable "pi", which the export does not carry either: create it before the
import, as a query variable listing the node tag's values.

Six dashboards are kept as files and built by
[docs/dashboards/build.py](dashboards/build.py). Import each in the InfluxDB
web interface: Dashboards, Create Dashboard, Import Dashboard. Each import
makes a dashboard of its own, so delete the old one first.

- [blind1-energy.json](dashboards/blind1-energy.json): the blind's energy,
  described below.
- [pi-health.json](dashboards/pi-health.json): every machine's health and
  the code it last installed, described under "Which code each machine last
  installed".
- [blind1-chins.json](dashboards/blind1-chins.json): the Chins pack from its
  own battery management system. The top row is the newest reading in the
  last hour, so a pack that has stopped answering shows empty rather than an
  old number. The status row shows the pack's charge and discharge switches,
  each alarm its protection word holds (cell or pack over or under volts,
  too hot or too cold to charge or discharge, over current, short circuit,
  a front end chip error, switches locked), the cells it is balancing, and
  how long ago it last answered. A switch reading OFF while an alarm shows
  is the pack protecting itself.
- [blind1-victron.json](dashboards/blind1-victron.json): the blind's charge
  controller. The top row is the newest reading in the last ten minutes. The
  controller reports every thirty seconds, so an empty cell means it
  stopped. The status row gives the charge state, the tracker, the error and
  the off reason in words. The yield by day comes from the lifetime total,
  as the daily energy query works it out.
- [garage-sok.json](dashboards/garage-sok.json): the garage's packs, one row
  each in the status table, with how long ago each last answered. Every
  device on the rack is a pack, so a pack added there appears with no edit.
- [garage-charge-inverter.json](dashboards/garage-charge-inverter.json): the
  garage's inverter, with whether its newest poll got a reading and its
  newest error, with how long ago that was.

## How much energy went into and out of the battery each day

Import [docs/dashboards/blind1-energy.json](dashboards/blind1-energy.json) in
the InfluxDB web interface: Dashboards, Create Dashboard, Import Dashboard.
It shows:

- where the load's power comes from, stacked: loads from the battery, loads
  from solar, and the surplus going into the bank. The lower two add up to
  the load. A window with no reading from either the shunt or the charge
  controller is left out, and the lines run straight across it.
- the state of charge underneath, on the same time axis, so a shed shows
  against the charge that caused it. The Chins's own figure is the one the
  shedding ladder follows. The shunt's is beside it; it counts amp-hours for
  the whole bank and drifts from the packs between full charges.
- the bank's average draw in the dark over the last 24 hours.
- daily energy for the bank (in and out) and for the system (solar and
  load), one flat step per day, and the full daily table.
- the current through each pack: the whole bank from the shunt, the Chins
  from its own reading, and the Weize pair as the difference, since it has
  no meter of its own.
- each pack's share of the bank's current, in percent, left out while the
  bank carries under two amps, and the Chins's share of each day's
  throughput as a flat step. A share above 100 or below 0 means one pack is
  charging the other. The share moves with state of charge, so a fixed
  share is not a sign of trouble by itself.
- what each load draws, as the controller last measured it. The controller
  measures a load as the step in the bank's current when it switches the
  load, with the charge controller reading fresh and showing no charge
  coming in and no other relay switching, and keeps
  the latest figure for each on its card, in the meter's state file. It
  publishes every figure on every pass, so the table reads only the last
  week and a figure stays until a newer one replaces it. A load the node
  file disables still has its figures published, since what it draws does
  not change. A load renamed or taken out of the node file keeps its old
  row until its entry is deleted from the state file and the controller
  restarted. Day and night are
  kept apart for the cameras, whose infrared lights draw at night, and a
  switch in twilight is not measured. The far cameras open each window
  five minutes after the near ones, so each group's start is a step of its
  own. The base load is the bank's mean draw over the ten minutes after
  every relay load has been off for ten: the Pi, the network gear, the radios and the
  recorder, which is wired around its relay. The pump is measured as it starts,
  with the inverter that starts just before it. The meter block in the node file names the
  shunt, the charge sources and the loads wired around their relays; take
  the recorder out of wired_around once its branch goes through its relay.
  The note beside the table holds values from tests made by hand. Add a row
  to `LOADS_NOTE` in [docs/dashboards/build.py](dashboards/build.py) after
  such a test, since an edit made in Cloud is lost when the dashboard is
  imported again. The query is
  [docs/queries/blind1-loads.flux](queries/blind1-loads.flux), and the
  meter is [monitoring/control/meter.py](../monitoring/control/meter.py).

The supply and state of charge graphs follow the dashboard's time range. The
daily cells start at the midnight on or before the range's start and run
through today. The
night draw is always the last 24 hours, and the load estimates the last 7
days. Pick 24 hours for the supply graph
and 7 days for the daily ones. A range reaching back past the bucket's oldest
data makes the first day a part day.

The daily cells are
[docs/queries/blind1-daily-energy.flux](queries/blind1-daily-energy.flux),
started from the dashboard's range. After changing that query, rebuild the
dashboard, delete the old one, and import it again; each import makes a
dashboard of its own:

```
python3 docs/dashboards/build.py
```

The daily table gives one row per local day, in watt-hours unless the name
says otherwise. The last row is today so far.

The shunt sits where the battery bank joins the rest of the system. Solar
that feeds a load directly never crosses it. So the shunt tells what the bank
did, and the charge controller is needed to tell what the loads used.

| Column | Meaning |
|---|---|
| `bank_in_wh` | Energy into the bank through the shunt, heater draw included. |
| `bank_out_wh` | Energy out of the bank through the shunt. |
| `bank_net_wh` | `bank_in_wh` less `bank_out_wh`. Heater energy is in it but is not stored, so on a cold day it reads high. |
| `ah_net` | The change in the shunt's remaining capacity. A cross-check on the sign of `bank_net_wh`. It flattens while the shunt is clamped at full or empty. |
| `solar_wh` | The charge controller's production, from its lifetime yield. |
| `load_wh` | Everything the loads used: `solar_wh` less `bank_net_wh`. Includes the wiring loss between the controller and the shunt. Leaves out heater energy. |
| `direct_solar_wh` | Solar the loads used without it crossing the shunt: `solar_wh` less `bank_in_wh`. |
| `night_load_wh` | Energy out of the bank while the controller produced nothing. A stretch with no shunt readings is left out, since it could hold daylight. |
| `night_hours` | How long the controller produced nothing. An overcast day is not night. |
| `night_avg_w` | `night_load_wh` over the time its readings cover: the load's average draw in the dark. A gap leaves out its energy and its time together, so it does not pull the average down. |
| `solar_wh_check` | The controller's output power, summed from its snapshots. It should land close to `solar_wh`. A large gap means missing controller readings. A controller outage across midnight puts its yield on the later day. |
| `shunt_coverage_pct` | Shunt readings received, of one a minute in the day so far. Well under 100 means the day had a gap. The energy across a gap is kept, but all of it is filed in the direction of the reading that ends the gap, and on that reading's day. |

A day's `load_wh` is what the loads drew at that day's state of charge. The
shedding ladder cuts the cameras and the recorder as the charge falls, and a
scheduled irrigation start needs the charge controller to have been in
absorption or float within the last six hours. A start that fails the check
is skipped, so a sunny day's load overstates what a dark spell draws. A run
started by hand skips the check.

## Which code each machine last installed

Every install leaves a record in `/var/lib/monitoring/installed.json`, and
the host device publishes it with each reading:

| Field | Meaning |
|---|---|
| `code_commit` | The commit installed, abbreviated. `unknown` when git could not read the checkout. |
| `code_branch` | Its branch. `HEAD` when the checkout was not on a branch, `unknown` when git could not read it. |
| `code_commit_time` | When that commit was made, in epoch seconds. |
| `code_dirty` | `1.0` when the checkout had local changes at install, so what was installed is not exactly that commit. `0.0` when it had none, `-1.0` when git could not read it. |
| `code_installed_time` | When the install ran, in epoch seconds. |

A machine with no record publishes none of these until an install writes
one, and an install that stops partway removes the record, so
none are published until an install finishes. A dashboard showing the latest
value of each still shows the previous install's for as long as it looks
back: a week in the health table, an hour in the query below. When the
commit reads `unknown`, a commit time beside it is from an earlier install.

The record says what was last installed, which is not always what runs. The
collectors telegraf runs on a schedule, the override page and the Wavlink
repair start fresh from the checkout each time, so after a pull they run the
pulled code at once, before any install. The controller and the collectors
that stay running keep the code they started with until something restarts
them, which an install does. So after an install every process runs the
installed commit, and a pull without one leaves the record behind the disk.

[docs/dashboards/pi-health.json](dashboards/pi-health.json) shows every
machine at once: a table of each one's latest readings, how long ago it last
reported, and the code it last installed, with graphs of temperature, load, memory,
swap, disk and uptime under it. It finds machines by their host readings, so
a machine added later appears on it by itself. Import it the same way as
the energy dashboard. A machine that has not reported for a week drops off
the table.

The code columns as a query of their own:

```
host = from(bucket: "raw")
    |> range(start: -1h)
    |> filter(fn: (r) => r.device == "host" and r._field =~ /^code_/)
    |> last()

text = host |> filter(fn: (r) => r._field == "code_commit" or r._field == "code_branch")

times = host
    |> filter(fn: (r) => r._field == "code_commit_time" or r._field == "code_installed_time")
    |> map(fn: (r) => ({r with _value: string(v: time(v: int(v: r._value) * 1000000000))}))

dirty = host
    |> filter(fn: (r) => r._field == "code_dirty")
    |> map(
        fn: (r) =>
            ({r with _value: if r._value > 0.5 then "yes" else if r._value < -0.5 then "unknown" else "no"}),
    )

union(tables: [text, times, dirty])
    |> group(columns: ["_measurement", "node"])
    |> pivot(rowKey: ["_measurement", "node"], columnKey: ["_field"], valueColumn: "_value")
    |> group()
    |> keep(
        columns: [
            "_measurement",
            "node",
            "code_commit",
            "code_branch",
            "code_commit_time",
            "code_dirty",
            "code_installed_time",
        ],
    )
```

## Sound is off on every node

The install masks PipeWire and its session manager, wireplumber, for every
user. wireplumber follows every Bluetooth scan and connection the pack and
shunt readers make, and its memory grows with them until the kernel kills it
or the machine stops answering. Nothing here plays sound.

To have sound back on a node:

```
sudo systemctl --global unmask pipewire.socket pipewire.service pipewire-pulse.socket pipewire-pulse.service wireplumber.service filter-chain.service
```

Then reboot, so every session starts them. The next install masks them
again.

## What to do when telegraf is down

```
systemctl status telegraf
journalctl -u telegraf -n 50
sudo systemctl restart telegraf
```

If it restarts and then stops again, or runs with nothing reaching the
database, check the credentials file's ownership. Telegraf fails silently
on the wrong one:

```
ls -l /etc/monitoring/influx.env
```

Expect `root:telegraf`, mode `640`. Anything else and telegraf starts,
logs one permission line, and sends nothing.

## When the blind's Wavlink has lost its settings

The blind's Wavlink can return to factory settings when its power fails. It then
broadcasts WAVLINK-N instead of blind1, and everything behind it drops off the
network. The downstairs Pi checks for this every five minutes and repairs it by
uploading a saved copy of the settings.

It acts only when blind1 is absent and WAVLINK-N is present on that unit's radio
address, at two checks in a row. It makes at most three attempts a day, at least
30 minutes apart.

```
sudo /opt/monitoring/bin/wavlink-recover status          # what it has tried
sudo /opt/monitoring/bin/wavlink-recover check --dry-run # what it would do
sudo /opt/monitoring/bin/wavlink-recover reset-attempts  # allow more attempts
journalctl -u monitoring-wavlink-recover -n 50
```

Before working on the Wavlink by hand, stop both the timer and any check already
running, or a repair can run while you are in the middle of it. Stopping a repair
this way records it as one failed attempt. Start the timer again when you finish.

```
sudo systemctl stop monitoring-wavlink-recover.timer monitoring-wavlink-recover.service
sudo systemctl start monitoring-wavlink-recover.timer
```

The configuration is /etc/monitoring/wavlink-recover.yaml, and the settings file
it names came from this unit. Both are root-only and neither is in this
repository. Take a fresh export after any change to the Wavlink. Never upgrade
this unit's firmware: it runs a custom build.

## What to do when a Pi will not boot

Check power first: a brownout that drops a Pi mid-write is a far more common
cause than a dead card. If the card itself is suspect, move to a spare card,
image it, and bring the machine back up with the install script, which is
safe to run again:

```
sudo /opt/monitoring/deploy/install.sh <site> <node>
```

It relinks `/etc/monitoring/node.yaml`, reinstalls telegraf's inputs, and
leaves any credentials already on the card untouched. It does not refuse to
finish because one sensor failed its check; a dead five-dollar sensor must
never be the reason a site stays dark.

---

## Irrigation draw

Measured at the shunt as the change in battery current, with the inverter on
and everything else steady. The shunt sees the whole bank, so each figure is
the step from the reading just before, not a reading of its own.

| State | Step in battery current | About |
|---|---|---|
| Inverter on, idle | 0.8 A | 10 W |
| Pump moving water | 44.7 A, steady | 600 W |
| Pump start | 56 A for one reading | |
| Pump running dry | 16.1 A, steady | 215 W |

Dry is about a third of wet, so a step under 30 A once the pump has started
means a dry pump or a lost prime. The pump drew nothing for its first few
seconds on one run, so a check waits for that before it judges.

## Which GPIO pin drives which load

| Machine | Load | GPIO | Physical pin | With no signal at all |
|---|---|---|---|---|
| blind1 / downstairs | cameras near | 27 | 13 | running (normally closed) |
| blind1 / downstairs | cameras far | 23 | 16 | running (normally closed) |
| blind1 / downstairs | nvr | 18 | 12 | running (normally closed) |
| blind1 / downstairs | irrigation: inverter | 25 | 22 | off (normally open) |
| blind1 / downstairs | irrigation: pump | 22 | 15 | off (normally open) |
| blind1 / downstairs | cpu fan | 24 | 18 | off (normally open) |
| blind1 / upstairs | cpu fan | 24 | 18 | off (normally open) |
| blind1 / upstairs | vent fan | 26 | 37 | off (normally open) |
| garage | battery heating | 24 | not recorded | off (normally open) |

Of these, the two camera groups, the recorder, the two CPU fans, the vent fan
and battery heating are load types the controller drives. Irrigation is declared
and deliberately held back, disabled in the node file. Battery heating is
disabled in the node file too, because its relay is not wired.

## Battery heating

The heater is decided by these rules, in this order. The `reason` field says
which one applied and, when it stayed off, which gate stopped it.

1. **Hard stop.** Any fresh sensor at or above `any_sensor_above_f` turns it off
   at once, past every timer. It holds until that sensor reads below the limit,
   so a sensor that goes quiet while hot keeps the heat off until a reboot. A
   `control override` to on does not get past this.
2. **Ordinary heat.** The coldest fresh sensor at or below `coldest_below_f`, with
   panel voltage fresh and above `above_volts`. It stops at or above
   `coldest_above_f`, or when production is lost. Between the two bounds it does
   what it was last commanded to do. Each change waits out `heat_dwell` or
   `stop_dwell`.
3. **Preheat.** Within `lead` before sunrise, a cold pack heats with no
   production, if the clock is trusted, every charge reading is fresh with the
   lowest above `soc_above_pct`, and the forecast is fresh and above its
   threshold.
4. **No temperature to trust.** With every sensor stale, or two further apart
   than `max_disagreement_f`, it heats on production alone as the plain heater
   did, bounded by the snap-disc cutout. With production unread too, it is off.

`max_run_per_day` caps all of that together, per local day, or per rolling 24
hours while the clock is untrusted. `cooldown` is the off time after a run ends,
and `min_off` still applies. An operator's force on gets past the cap and the
cooldown, and the time it runs still counts. The count is kept in tmpfs, so a
reboot starts it again from zero. The shedding switch does not touch this load.

The tape probably has a thermostat of its own, so `powered` at `1.0` says what
was commanded, and the tape drawing nothing then is normal, not a fault. Nothing
reads the draw back.

The forecast comes from the `open_meteo` device, which needs the internet and
calls the service at most once an hour. If it fails it publishes `ok=0.0` with
the reason, and its last good reading goes on aging in the cache, so a forecast
past its `stale_after` stops preheat and nothing else.

## Published fields, per load

Every load the controller drives is its own point, carrying the device's
tags and a `load` tag naming the load, such as `load=cameras_near`. The field
names are the same for every load, so one chart over `powered` repeats over
loads and a table over all loads needs no column per load. A load added to
the node file needs no panel edited.

| Field | Meaning |
|---|---|
| `powered` | `1.0` if the load has power right now, `0.0` if not. Read this, not `level`, to know the load's actual state. |
| `level` | Whether the relay's coil is energized: `1.0` energized, `0.0` not, whichever voltage on the pin does it. Opposite of `powered` on a normally-closed load such as the cameras. |
| `shed_level` | How many tiers are currently latched. `0` is nothing latched. |
| `reason_code` | Which kind of rule decided this pass, as a number. Table below. |
| `reason` | The same decision in words, with the number it compared. |
| `decided_on` | That number by itself, for a quick look or a graph. Absent, not zero, when no value informed the decision, so a query must allow for it being missing. |
| `schedule_suspended` | `1.0` if a latched tier's schedule could not be judged because the clock is not trusted. |
| `floor_latched` | `1.0` while a voltage floor holds this load off. It stays `1.0` after the reading has climbed back above the shedding bound, until it clears the restoring bound. `0.0` otherwise, including on a load with no floor. |
| `floor_latched_by` | Which source holds the floor, as `device.field`, such as `bms.cell_min_volts`. Only that source can release it, so this is the number to watch. The one exception: a source marked `fallback` speaks only while the primary sources cannot be read, and once a primary reads again it judges a shed a fallback took. Absent while `floor_latched` is `0.0`. |
| `level_changes` | How many times the level on this load's pin has changed since the machine booted. A counter, not a state: two changes are one cycle of the load, so "how many times did the recorder cycle today" is half the difference between its values at the two ends of the day. It is kept in tmpfs with the rest of the controller's state, so it survives a restart of the service and starts again at zero on every boot; a reboot inside the day shows as the count dropping back to zero. A drive that energizes the relay counts, and so does one that releases it. A restart of the service while the relay was energized counts twice, because the relay drops when the service stops and is energized again when it starts. A decision that never reached the pin does not count. Not to be confused with a battery's `cycles`, which counts charge cycles. |

An irrigation load is two points: `load=irrigation` for the pump and the run,
and `load=irrigation.inverter` for the inverter. The first also carries:

| Field | Meaning |
|---|---|
| `phase` | `0` idle, `1` starting, `2` running, `3` stopping. |
| `amp_hours_today` | The pump's draw today, counted from the shunt. |
| `failed_starts_today` | Failed starts today. Three lock it out until the next day. |
| `locked_out` | `1.0` while it is locked out. |

A few fields describe the controller itself, not any one load. They are on
the point with no `load` tag:

| Field | Meaning |
|---|---|
| `clock_synced` | `1.0` if the clock is trusted this pass. |
| `clock_reason` | Why it is, or is not. |
| `passes` | How many decision passes the controller has made since it started. |
| `loop_monotonic` | Its own uptime clock, in seconds. Not a wall time. |
| `published_age_seconds` | How long ago the controller wrote what the collector just read, measured by the collector. A loop that has wedged shows here as an age that keeps growing while every other field holds still. |

When the controller has published nothing, or what it published is from a
previous boot, the collector writes one point with `ok=0.0` and the reason
in `error_message`, on the series with no `load` tag, and every load's
series stops. A gap in a load's series is therefore always that failure or
a collector fault, never a load with nothing to say.

## Reason codes

| Code | Name | Meaning |
|---|---|---|
| 0 | none | Nothing is shedding this load. |
| 1 | state of charge | A tier's percentage threshold is in effect. |
| 2 | floor | A voltage floor tripped. This overrides every other rule, including an override and the shedding switch. |
| 3 | holding stale | No reading can be trusted; holding the last known state rather than guessing. |
| 4 | override | An operator forced this with `control override`. |
| 5 | shedding disabled | `control shedding off` is holding this load on despite its tiers. |
| 6 | schedule suspended | A latched tier has a schedule, and the clock cannot be trusted to judge it. |
| 7 | no reading | A fan or heater lacks a reading it needs. A CPU fan runs if it has had a reading since the machine booted, and stays off if it has not. A vent fan stops. A heater with no temperature to trust heats on production alone, and a heater with no fresh production reading does not heat unless a preheat lets it. |
| 8 | thermal | A fan or heater decided on its temperatures: at or above its upper bound, at or below its lower bound, or between them and keeping its last state. A heater's hard stop and its preheat are in here too; `reason` tells them apart. |
| 9 | refused | The node file declares a load type this controller does not drive. |
| 10 | held by timer | The decision is correct, but a minimum run or minimum off time has not elapsed, so it has not taken effect yet. Also a heater that wants heat with its daily cap used up or its cooldown still running. |
| 11 | run refused | An irrigation run cannot start: a start condition fails, the day's amp-hours are used, or it is locked out. `reason` says which. |
| 12 | failed start | An irrigation run stopped because the pump drew outside its range, or no shunt reading came in time to check it. It counts toward the lockout. |
