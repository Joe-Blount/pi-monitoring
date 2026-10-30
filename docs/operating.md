# Operating the system

For the person standing at a Pi who needs to change something and cannot ask
anyone first. Each heading is a question. Under it is the exact command.

`<file>` means the node file for the machine you are standing at. On an
installed machine that is always:

```
/etc/monitoring/node.yaml
```

`<load>` means a load's name from that machine's `control:` block, such as
`cameras`. Find the names for this machine with:

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

## Force the cameras on right now

```
sudo control override cameras on --for 8h
```

It runs for the time you gave and then returns to normal control on its own.
Clear it sooner:

```
sudo control override clear cameras
```

## Force the cameras off

```
sudo control override cameras off --until "YYYY-MM-DD HH:MM"
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

Do not read `level` as whether the cameras have power. It is the raw voltage
on the pin, not the load's state, and for a normally-closed branch the two
are opposite: `level=0.0` next to `powered=1.0` means the relay is
de-energized and the cameras are running, which is correct. Use `powered`
for whether the load has power.

In the database the same fields carry the same names, on a point tagged
`load=cameras`. See the table of published fields below.

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

## Which GPIO pin drives which load

| Machine | Load | GPIO | Physical pin | With no signal at all |
|---|---|---|---|---|
| blind1 / downstairs | cameras | 23 | 16 | running (normally closed) |
| blind1 / downstairs | irrigation: inverter | 25 | 22 | off (normally open) |
| blind1 / downstairs | irrigation: pump | 22 | 15 | off (normally open) |
| blind1 / downstairs | cpu fan | 24 | 18 | off (normally open) |
| blind1 / upstairs | cpu fan | 24 | 18 | off (normally open) |
| blind1 / upstairs | vent fan | 26 | 37 | off (normally open) |
| garage | battery heating | 24 | not recorded | off (normally open) |

Of these, `cameras` and the two CPU fans are load types the controller
drives. Irrigation and the vent fan are declared and deliberately held back,
disabled in the node file. Battery heating is declared for its relay and
wiring, but is not yet a load type this controller drives. `blind1 /
downstairs` itself is not built yet; its pin assignments are the plan for
when it is.

## Published fields, per load

Every load the controller drives is its own point, carrying the device's
tags and a `load` tag naming the load, such as `load=cameras`. The field
names are the same for every load, so one chart over `powered` repeats over
loads and a table over all loads needs no column per load. A load added to
the node file needs no panel edited.

| Field | Meaning |
|---|---|
| `powered` | `1.0` if the load has power right now, `0.0` if not. Read this, not `level`, to know the load's actual state. |
| `level` | The raw pin voltage: `1.0` energized, `0.0` not. Opposite of `powered` on a normally-closed load such as the cameras. |
| `shed_level` | How many tiers are currently latched. `0` is nothing latched. |
| `reason_code` | Which kind of rule decided this pass, as a number. Table below. |
| `reason` | The same decision in words, with the number it compared. |
| `decided_on` | That number by itself, for a quick look or a graph. Absent, not zero, when no value informed the decision, so a query must allow for it being missing. |
| `schedule_suspended` | `1.0` if a latched tier's schedule could not be judged because the clock is not trusted. |
| `floor_latched` | `1.0` while a voltage floor holds this load off. It stays `1.0` after the reading has climbed back above the shedding bound, until it clears the restoring bound. `0.0` otherwise, including on a load with no floor. |
| `floor_latched_by` | Which source holds the floor, as `device.field`, such as `bms.cell_min_volts`. Only that source can release it, so this is the number to watch. Absent while `floor_latched` is `0.0`. |
| `level_changes` | How many times the level on this load's pin has changed since the machine booted. A counter, not a state: two changes are one cycle of the load, so "how many times did the recorder cycle today" is half the difference between its values at the two ends of the day. It is kept in tmpfs with the rest of the controller's state, so it survives a restart of the service and starts again at zero on every boot; a reboot inside the day shows as the count dropping back to zero. A drive that energizes the relay counts, and so does one that releases it. A restart of the service while the relay was energized counts twice, because the relay drops when the service stops and is energized again when it starts. A decision that never reached the pin does not count. Not to be confused with a battery's `cycles`, which counts charge cycles. |

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
| 7 | no reading | A CPU fan has no fresh temperature. It runs if it has had a reading since the machine booted, and stays off if it has not. |
| 8 | thermal | A CPU fan decided on its temperature: at or above its upper bound, at or below its lower bound, or between them and keeping its last state. |
| 9 | refused | The node file declares a load type this controller does not drive. |
| 10 | held by timer | The decision is correct, but a minimum run or minimum off time has not elapsed, so it has not taken effect yet. |
