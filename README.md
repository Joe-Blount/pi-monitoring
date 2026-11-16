# pi-monitoring

Monitoring and load control for off-grid solar sites, running on Raspberry Pis
and writing to InfluxDB.

Two sites today: a deer blind with a Victron charge controller, a battery bank
and a camera system, and a garage with an EG4 inverter. A third site is
expected, which is why the configuration is per machine rather than hard coded.

## Shape

Three layers, with one narrow seam between them.

```
   node.yaml                      what this machine has, and what to do with it
       │
       ▼
   drivers  ────────────────────► InfluxDB line protocol on stdout
   (this repository)                        │
                                            ▼
                                     agent: telegraf
                                interval, buffer, retry, batch
                                            │
                                            ▼
                                     InfluxDB Cloud 2
```

**The seam is line protocol on stdout.** A driver reads one device and prints
line protocol. That is the whole contract. Every driver therefore runs by hand,
tests against recorded data, and is developed on a laptop with no network, no
credentials and no database.

**Telegraf is the agent**, not something this project reimplements. Scheduling,
buffering through an outage, retry, batching and authentication are exactly the
parts that are tedious to write and easy to get wrong, and the way they go
wrong is quiet: an authentication failure that nothing surfaces can discard
readings for an unknown length of time while every machine looks healthy.

**Control is separate from telemetry.** On a site that switches loads, the
controller is its own supervised service, not a telegraf input. It decides
from readings taken on its own machine: on a node with a control block, each
collector also leaves its latest good reading in /run/monitoring/readings, and the controller reads those files
rather than any device. What it decided goes out as a small document that the
`control` driver publishes through telegraf like any other reading. Load
shedding that depends on the network fails exactly when the power is failing.

## Repository

```
monitoring/
  lineproto.py       build and escape line protocol
  config.py          load and validate a node file
  calibration.py     scale and offset, applied in one place
  duration.py        read durations such as 10m from node files
  runner.py          run a node's devices and print what they read
  drivers/           one module per device family
  wavlink_recover.py restore a Wavlink that has returned to factory settings
  control/           the load controller: decisions, relays, the override page
    decide.py        the decision: a snapshot in, what each load should do out
    loop.py          the service: reads, decides, drives the relays, publishes
    meter.py         what each load draws, measured where it is switched
    irrigation.py    the inverter and pump sequence
    sun.py           sunrise and sunset, for windows measured from them
    page.py          the override page
bin/
  collect            read devices, print line protocol
  control            run the load controller, force loads, inspect decisions
  control-page       serve the override page
  wavlink-recover    the Wavlink repair, as its timer runs it
  ble-discover       enumerate an unknown Bluetooth device
  gpio-probe         measure what a pin does when software lets go
docs/
  bringup.md         bringing up a machine
  operating.md       day-to-day operation, a heading per question
  dashboards/        dashboards built by build.py, and cloud/ as Cloud exports them
  queries/           Flux queries: daily energy in and out, and what each load draws
sites/
  example/node.yaml  device options, with the reasoning
  blind1/            upstairs.yaml, downstairs.yaml, telegraf.d/
  garage/            garage.yaml, telegraf.d/
deploy/              install script, systemd units, the shared telegraf inputs
tests/               fixtures and tests, none of which need hardware
influx.env.example   how to install the database credentials
location.yaml.example
                     where the machine is, for sun times; the real file lives
                     in /etc/monitoring, because this repository is public
```

## Configuration

One file per **machine**, not per site. A site can have several machines: the
deer blind has an upstairs node with the sensors and a downstairs node with
the power devices and the load control. Both write the same
measurement, so dashboards do not care how the work is divided.

`sites/example/node.yaml` documents the device options and explains the ones
whose reasoning is not obvious. Start there for devices. Its DC branch load
is older than the shed ladder, so the controller would refuse that control
block; its heater, irrigation and Pi fan loads are current. For a working
ladder, load meter, vent fan and spare pins, read `sites/blind1/downstairs.yaml` and
`sites/blind1/upstairs.yaml`.

Each device declares a mode:

| Mode | Runs as | For |
|---|---|---|
| `poll` | one `inputs.exec` per node, and a slow one where a node has slow devices | stateless reads: 1-Wire, I2C, host stats, Bluetooth packs |
| `resident` | `inputs.execd` | streaming or counting: a rain gauge, unprompted serial frames |

Two modes, and a device that admits exactly one reader still uses one of them.
A serial port admits one reader; so does each Bluetooth connection. Load
control therefore reads what a collector published rather than opening any
device itself, which is the only arrangement where the control loop and the
collectors cannot compete for a port. The alternative, letting the controller
own single-reader devices, means a node where control is switched off silently
stops reading its own battery.

## Conventions

These are not style preferences. Each one is here because its absence caused a
real problem.

- **Numeric fields are always floats.** InfluxDB rejects a field whose type
  changes between writes.
- **Yes-or-no values are published as 0.0 and 1.0, not booleans**, so a daily
  mean reads directly as the fraction of the day a condition held.
- **A reason is a field, never a tag.** Fields are not indexed, so a changing
  string costs nothing; the same string as a tag creates a series per value.
- **One unit system.** Temperatures are Fahrenheit, converted in the driver,
  never in the dashboard.
- **Devices are addressed by stable identity**: the `by-id` path for USB
  serial, the `28-*` id for 1-Wire, the address for I2C, the MAC for
  Bluetooth. Never `/dev/ttyUSB0`, whose numbering moves between boots.
- **No secrets in this repository, and no coordinates.** Credentials live in
  `/etc/monitoring/influx.env`, mode 640, owned by root with group telegraf
  so telegraf can read it. See `influx.env.example`. A machine's position
  lives in `/etc/monitoring/location.yaml`, never in a node file.
- **Shedding is a ladder of tiers on state of charge, with a floor beneath
  it.** A fresh primary floor source can shed a load, and only the source
  that shed it can release it. Pack voltage is only a fallback source,
  because it sags when a large load starts and would shed a healthy bank: a
  fallback counts only while no primary source reads, and a primary that
  reads again judges a shed a fallback took. A stale reading holds every
  tier latch as it was, and the latched tiers' schedules still set the
  running times. A floor a primary source shed stays shed while that
  source is quiet.
- **Verifying what the hardware did is optional.** It can be derived from the
  measured DC load, since the switched loads differ by orders of magnitude, so
  no extra wiring is needed. Where a node has a load meter, it measures and
  publishes that step on each clean switch; nothing uses it to confirm a
  switch. Only the pump's start is checked against an expected band, by the
  dry-run check's own reading of the same step. Restart behaviour never
  depends on it: the state after a released pin is determined by the relay
  polarity.
- **Silence always means failure, never intent.** Every declared device
  publishes something every interval; one that is `enabled: false` publishes
  `enabled=0.0` and nothing else. A device this machine does not have is
  absent from the configuration instead, which is a different statement.
- **A polled device that cannot be read publishes that it failed**, with the reason
  as a field, rather than publishing nothing. A gap in a graph is ambiguous;
  `ok=0.0` beside an error string is not.
- **Every device is tagged with its own name**, so two devices sharing a
  location cannot write into one series and silently overwrite each other.
- **A failed write to the database is logged, never discarded.**
- **Counters persist.** Anything that accumulates, such as a rain total or a
  lockout counter, survives a restart in a state file, because the thing it
  protects against is usually a restart. The exceptions are the heater's daily
  run time and each load's level_changes, which live in tmpfs and start
  again at zero after a reboot.

## Testing

Nothing in the test suite needs hardware, a network or credentials.

- Drivers are tested from recorded device output. The charge controller's
  checksum byte can itself be a carriage return, line feed or tab, which breaks
  naive line-based parsing about one frame in 85; that case is a fixture so it
  cannot regress.
- The configuration loader is tested by asserting that a malformed node file
  fails at load with a clear message, rather than at three in the morning on a
  Pi nobody can reach.
- The control decision logic is a pure function, snapshot in and decision out,
  so a test states a situation and asserts the outcome without waiting for
  weather.
- Relay polarity is asserted as the voltage on the pin, read through
  gpiozero's mock pin factory, never as the state the controller wanted. The
  library's own `active_high` inverts the claim and every level after it, and
  `on()` is logical rather than electrical, so a double that records calls
  cannot see the difference. On a Pi the controller claims each relay pin
  through its own lgpio factory, already at the level that leaves its coil
  off, in one call, so no pin passes through the wrong level on the way.
- The load meter is tested by replaying passes of shunt readings, charge
  readings and switches, including the ones that must not yield a figure:
  charge arriving, two relays at once, a reading stamped too soon after a
  switch.

## Running it

```
collect --node sites/garage/garage.yaml --list     what this node declares
collect --node ... --check                         probe devices, take no reading
collect --node ... --poll                          every poll-mode device, once
collect host --node ...                            one device
collect mppt --node ... --stream                   a resident device, forever
collect <device> --node ... --raw                  unparsed device output
```

`--poll` is what telegraf runs. `--speed fast` runs every thirty seconds.
`--speed slow` runs the devices whose read can take tens of seconds, which
today are the Bluetooth ones and the forecast. It runs every three minutes by
default, and a node file can set `slow_poll_interval`; the blind's downstairs
node uses one minute. A Bluetooth read can take longer than the fast
input's twenty-second timeout, and a kill at the timeout discards every
reading that process took, so the slow devices get a process of their own,
with a longer timeout.

Exit codes for `--poll`: 0 when anything was written, failure points
included, 1 when nothing was, 2 when the node file itself is wrong. A single
dead sensor among several does not mark the collection failed, because one
broken sensor must not stop a site reporting. A single-device read and
`--check` exit 1 when the device failed.

## Installing on a machine

Step by step, including the kernel overlays each sensor needs and how to
confirm data is arriving: **[docs/bringup.md](docs/bringup.md)**.

Once it is running, day-to-day commands for forcing a load on or off, seeing
why one is shed, and recovering from a dead telegraf or a Pi that will not
boot: **[docs/operating.md](docs/operating.md)**.

In short:

```
sudo deploy/install.sh <site> <node>
sudo deploy/install.sh garage garage
```

Idempotent, so updating is a fetch, a fast-forward merge, and the same
command again:

```
cd /opt/monitoring
sudo git fetch origin main && sudo git merge --ff-only FETCH_HEAD
sudo deploy/install.sh <site> <node>
```

A machine with no route to GitHub can fetch from a `git bundle` copied to it
instead; the bring-up guide has the steps. Of telegraf's configuration,
the install script installs **inputs** only and never touches the output, so a machine that already writes
to InfluxDB keeps its existing configuration and its existing credentials.

Poll devices share one `exec` input, common to every machine, and a slow one
on a machine that declares a slow device. Resident devices need one `execd` stanza each, committed per site under `sites/<site>/telegraf.d/`.
A test asserts that those fragments and the node file agree, because a resident
device with no stanza never runs and nothing reports it -- it simply never
appears in the data.

A device that fails its check produces a warning, not a refusal. A dead five
dollar sensor must never block a software update at a site nobody can reach.

## Continuous integration

Every push runs the suite on Python 3.9, 3.11 and 3.13, one for each
operating system release deployed, and loads every committed node file.
Linux runners only.

## Development

```
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest
```

No hardware, no network, no credentials.

## Drivers

One module per protocol, in `monitoring/drivers/`. Name a driver in a node
file and it is loaded lazily, so a machine with no Bluetooth device never
imports a Bluetooth stack.

| Driver | Reads | Link | How the protocol was established |
|---|---|---|---|
| `host` | the machine itself | none | Temperature, uptime, disk, address, processor load, available memory, and which code is installed. Load averages rather than a percentage, because a collector that runs once and exits cannot measure a rate. |
| `ds18b20` | one-wire temperature | GPIO | Kernel one-wire bus. Addressed by sensor id, so no node file mentions which GPIO carries the bus. |
| `dht` | DHT11 and DHT22 | GPIO | Read through the kernel's industrial I/O driver rather than by bit-banging from Python, so the microsecond timing is protected from the scheduler. Needs `dtoverlay=dht11,gpiopin=N`, and the overlay is called dht11 whatever sensor is attached. |
| `rain_gauge` | tipping bucket | GPIO | Pulse counter with the total persisted across reboots. Needs the lgpio pin backend: the sysfs fallback does not release the pin, so every restart finds it busy. |
| `vedirect` | Victron MPPT | serial | VE.Direct, documented by Victron. Frames are checksum-validated and a line that delivers bytes without ever completing a frame is bounded, which an idle timer alone does not catch. |
| `pi30` | Voltronic-protocol inverters | serial | The PI30 ASCII protocol, documented by several vendors. Written and tested against recorded output, but not yet against hardware: the EG4 here does not speak it. See `eg4_modbus`. |
| `abc_bms` | SOK and other ABC-BMS packs | Bluetooth | Protocol reverse engineered for interoperability by others and reimplemented here, not copied. The published cell-voltage layout was wrong for a 16-cell rack pack, so the reader validates its own cell numbering and publishes nothing rather than a set shifted by a byte. |
| `jbd` | Chins and other JBD packs | Bluetooth | Identified as JBD from its characteristic UUIDs before anything was sent. Big-endian, and one reply arrives across several notifications, so replies are reassembled by their length byte. |
| `qinglan` | QinLan coulomb-counting shunts | Bluetooth | No published protocol and nothing findable online. Reverse engineered for interoperability from the vendor's own Android application, then confirmed against the hardware. Nothing is copied from that application: this is an independent implementation of a wire format. Every request carries a bus address, and without it the device ignores you in silence and looks dead. |
| `eg4_modbus` | EG4 all-in-one inverters | serial | Modbus RTU at 9600, not the PI30 ASCII at 2400 that the model is usually said to speak. The register map was built by changing one load at a time and watching, including disconnecting the array, which is the only way to tell solar power apart from load power while solar is carrying the load. |
| `open_meteo` | the morning's forecast radiation | internet | The public Open-Meteo forecast, asked for shortwave radiation rather than cloud cover, because a real answer showed 100 percent cloud with 553 W/m2. Called at most once an hour, with the position from the machine's location file rounded to a tenth of a degree. A failure publishes `ok=0` like a dead sensor, and the last good number ages out of the cache meanwhile. |
| `wavlink_repair` | the Wavlink repair's attempts | a file | Reads the repair's own record and publishes when the last attempt was, how it ended, when the unit was last restored, and how many attempts fell in the last day. A missing record publishes `ok=0`, because it means no repair check has run. |
| `control` | the load controller itself | none | Not a device. The control loop is its own service and writes a small document; this reads it so what it decided, and what the load meter last measured, reaches the database through the same path as everything else. A missing document, or one from before this boot, fails loudly here. A loop that stops after writing one shows as a `published_age_seconds` that keeps growing, which is worth an alert, because once the process is gone every load sits at its fail-safe and nothing is managing the battery. |

Every driver is read-only. Several of these battery modules accept
unauthenticated commands that switch their charge and discharge paths, and
this project sends none of them.

Two habits are worth copying if you add one. Publish nothing rather than
something wrong: most drivers range-check their fields and refuse a reading
whose own numbers contradict each other, because a wrong number in a
database outlives the session that produced it. And confirm a field against
something independent before trusting it. The inverter driver checks that
volts times amps equals watts on every read, which is what catches a
register map shifting under a firmware update, since every individual value
stays plausible when that happens.

`collect --raw <device>` prints a device's unparsed output, which is where
to start when adding hardware or confirming a field.

## Status

Telemetry: written, tested and running at two sites. Every hardware driver in the table
above has read real hardware except `pi30`, which is written and tested against
recorded output but has not met an inverter that speaks it.

Not everything is decoded. The shunt's accumulated-totals record is unread,
and its twenty-five write commands are identified but unmapped, which is why
none of them is sent. On the inverter, the AC output current and apparent
power registers are right under load and disagree near zero, and the settings
block including the charge setpoints is unmapped. Each driver's docstring
separates what was confirmed against an independent reading from what is
inferred, which is worth reading before trusting a field.

Control: written and tested for five load types.

- **A DC branch**, such as a camera group, shed on the battery's state of
  charge through a ladder of tiers, with a voltage floor beneath it. Each tier
  is a complete schedule: on by the clock, on in windows measured from
  sunrise and sunset, or never. The load runs only while every latched tier
  permits it, so a deeper tier can only remove running time. A tier releases
  only once the battery clears its own recovery point, and a minimum run and
  off time keep a branch from cycling.
- **A fan aimed at the Pi**, run on the chip temperature with a band between
  on and off.
- **A vent fan**, run when the inside is warm and the outside air is cooler.
- **A battery heater**, run on the coldest pack sensor when the panels are
  producing or a good morning is coming, with a hard stop, a daily cap and a
  fallback to production alone when no temperature can be trusted.
- **Irrigation**: an inverter and a pump behind it, switched in order, with a
  dry-run check on the shunt, run limits, battery limits and a daily lockout
  after failed starts.

Where each one runs:

| Machine | Loads | Control |
|---|---|---|
| blind1 downstairs | near cameras, far cameras, recorder, Pi fan, irrigation | on |
| blind1 upstairs | vent fan, Pi fan | off in its node file |
| garage | battery heater | off; the heater's relay is not wired |

Downstairs, the recorder's branch is wired around its relay for now, so
switching that relay moves nothing. Irrigation is disabled in the node file.
A supervised wet and dry run on site measured the pump at 44.7 A moving
water and 16.1 A running dry, about 46 A and 17 A with its inverter. It
stays disabled until two things change. The dry-run check runs once, on the
first shunt reading after the pump settles, and has to run on every reading.
And the 12.6 V at which a running pump stops is too high: the battery sags
below it under the pump's draw, so a run on the battery alone stops at the
next charge controller reading.

The controller unit has a watchdog: if passes stop, systemd kills the
process, every pin is released, and every load falls to its wired fail-safe.
A small page on port 8088 shows each load's mode and plan in plain words and
lets a phone force one on or off for a set time; it answers only loopback,
private, link-local and tailnet addresses. From a shell, `control` forces a
load with `--for` or `--until`, stops tier shedding without stopping the
controller (the floor still sheds), clears an irrigation lockout, and shows with `dry-run` what one
pass would decide without touching a pin.
**[docs/operating.md](docs/operating.md)** has each command under the
question it answers.

**The load meter.** The bank's shunt sees every load at once, so the
controller measures one load as the step in the bank's current across a
switch of that load's own relay, which it made and knows the moment of. A
step counts only when nothing else could have moved the current: no other
relay switched, no charge coming in, and a starting reading taken well after
any earlier switch. Camera branches keep a night figure and a day figure,
because their infrared lights draw at night. The pump keeps one each for
moving water, running dry and above its expected band. The base load, which
is everything the controller cannot shed plus any load wired around its
relay, is read after every switched load
has been off for ten minutes with no charge. The latest figures are kept on
the card, republished every pass, and shown on the blind's energy dashboard.

**Dashboards.** Six dashboards are built from
[docs/dashboards/build.py](docs/dashboards/build.py) and kept as files. The
ones made by hand in InfluxDB Cloud are kept in
[docs/dashboards/cloud](docs/dashboards/cloud) as Cloud exports them.
[docs/queries](docs/queries) holds the daily energy query and the load
figures query. operating.md says how to import them and what the built ones
show.

The blind's Wavlink can return to factory settings when its power fails. A
timer on the downstairs node checks every five minutes and restores its saved
settings when it finds the unit in that state.

Driving a relay safely rests on one claim the software has to get right:
that killing a process releases its GPIO line, which depends on the gpiozero
backend. The controller also refuses to start on a kernel that keeps outputs driven after their
process exits, since that would defeat the fail-safe.

`bin/gpio-probe` measures it on any machine that will run the
control, with the operating system it will run, and needs nothing wired:

```
sudo ./bin/gpio-probe --pin <an unused pin> --node /etc/monitoring/node.yaml
```

It drives a pin, kills the process holding it, and reads the pin back without
claiming the line, which is the only way to see what was left behind. It
refuses a pin that a node file declares, that the board reserves, or that a
device tree overlay has taken -- the last of those matters because a one-wire
sensor is addressed by its id, so no node file ever mentions that GPIO4 is
carrying a bus.

## Protocols, credit and scope

**Purpose.** This exists so that equipment somebody owns can be read by
software they run, on their own premises, over a local link. That is
interoperability, and it is the only reason any protocol here was worked
out.

Every battery, inverter, charge controller and sensor is read over a local link to hardware the
owner owns: a serial cable, a GPIO pin, or Bluetooth. The one exception is
the forecast, which comes from a public service. Nothing talks to a vendor's
cloud service, and nothing needs an account.

No vendor material is redistributed. There is no vendor code here, no
application binary, no decompiler output and no vendor document. What is
published is an independent implementation of a wire format, written from
observed behaviour and confirmed against hardware. Protocols are methods of
operation, and a description of one is not a copy of anyone's software.

Every driver is read-only. Several of these battery modules accept
unauthenticated commands that can switch their charge and discharge paths, and
this project sends none of them. Switching is done with relays on circuits the
owner wired, never by asking a battery to disconnect itself.

Some of these protocols are published by the vendor, and those are the pleasant
ones. Victron documents VE.Direct and JK publishes its RS485 protocol. Others
were worked out by people reading their own traffic, and this project
reimplements what they documented rather than copying their code. No vendor
document is redistributed here; where one exists, it is cited and left where
the vendor put it.

A vendor document is not the same as the protocol in the box. The EG4 here
answers nothing to the PI30 protocol its model is usually said to speak, at any
rate from 1200 to 115200 baud, and answers Modbus RTU at 9600 immediately. A
unit that ignores the protocol you expect looks exactly like a dead cable, so
it is worth trying the other one before suspecting the wiring.

The QinLan shunt protocol appears to be undocumented anywhere public. What is
in `qinglan.py` was recovered from the vendor's own application and confirmed
against hardware, and is offered here because nobody else seems to have
published it.

The ABC-BMS driver owes its resolution of the cell-voltage paging to two
projects worth reading if you are adding a battery: batmon-ha, under the MIT
license, and aiobmsble, under Apache-2.0.

A driver is named after the protocol, not the brand. The same protocol appears
under many brands, and one brand can ship two unrelated protocols across its
range, so a brand name on a driver is wrong as soon as the range grows. Where a
brand name was used first, it stays as an alias so that existing node files keep
working.

## License

MIT. See LICENSE.

This software reads batteries and switches loads, and it is offered with no
warranty of any kind. Do not rely on it for anything where a missed reading or
a stuck relay would be unsafe. Treat every protective device in the system as
the thing that keeps you safe, and treat this as the thing that tells you what
happened.
