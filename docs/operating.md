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

Every load currently shed by a tier is handed back immediately. Check the
state with:

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

Inside, look for three fields named for the load:

```
cameras_reason          text: which rule fired, and the number it compared
cameras_reason_code     the same rule, as a number (table below)
cameras_decided_on      the bare number the rule compared, by itself
```

`cameras_reason` reads something like `state_of_charge_pct=9.4 below 12%`.
That number is the one that turned the load off. `cameras_shed_level` says
how many tiers are currently latched; `0` means none and the load should be
running unless something else is holding it off.

Do not read `cameras_level` as whether the cameras have power. It is the
raw voltage on the pin, not the load's state, and for a normally-closed
branch the two are opposite: `cameras_level=0.0` next to
`cameras_powered=1.0` means the relay is de-energized and the cameras are
running, which is correct. Use `cameras_powered` for whether the load has
power.

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
| blind1 / upstairs | vent fan | 26 | 37 | off (normally open) |
| garage | battery heating | 24 | not recorded | off (normally open) |

Of these, only `cameras` is a load type the controller currently drives.
Irrigation is declared and deliberately held back until it is enabled in the
node file. The vent fan and battery heating are declared for their relays and
wiring, but are not yet a load type this controller drives. `blind1 /
downstairs` itself is not built yet; its pin assignments are the plan for
when it is.

## Published fields, per load

Every load the controller drives publishes these, named with the load
first, such as `cameras_powered`:

| Field | Meaning |
|---|---|
| `<load>_powered` | `1.0` if the load has power right now, `0.0` if not. Read this, not `_level`, to know the load's actual state. |
| `<load>_level` | The raw pin voltage: `1.0` energized, `0.0` not. Opposite of `_powered` on a normally-closed load such as the cameras. |
| `<load>_shed_level` | How many tiers are currently latched. `0` is nothing latched. |
| `<load>_reason_code` | Which kind of rule decided this pass, as a number. Table below. |
| `<load>_reason` | The same decision in words, with the number it compared. |
| `<load>_decided_on` | That number by itself, for a quick look or a graph. |
| `<load>_schedule_suspended` | `1.0` if a latched tier's schedule could not be judged because the clock is not trusted. |

A few fields describe the controller itself, not any one load:

| Field | Meaning |
|---|---|
| `clock_synced` | `1.0` if the clock is trusted this pass. |
| `clock_reason` | Why it is, or is not. |
| `passes` | How many decision passes the controller has made since it started. |
| `loop_monotonic` | Its own uptime clock, in seconds. Not a wall time. |

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
| 7 | no reading | Reserved. Not produced by any load type this controller drives yet. |
| 8 | thermal | Reserved. Not produced by any load type this controller drives yet. |
| 9 | refused | The node file declares a load type this controller does not drive. |
| 10 | held by timer | The decision is correct, but a minimum run or minimum off time has not elapsed, so it has not taken effect yet. |
