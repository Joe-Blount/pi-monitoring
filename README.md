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
controller is its own supervised service, not a telegraf input. It reads only
devices attached to its own machine and emits its readings as datagrams.
Load shedding that depends on the network fails exactly when the power is
failing.

## Repository

```
monitoring/
  lineproto.py       build and escape line protocol
  config.py          load and validate a node file
  calibration.py     scale and offset, applied in one place
  drivers/           one module per device family
bin/
  collect            read devices, print line protocol
  gpio-probe         measure what a pin does when software lets go
sites/
  example/node.yaml  every option, with the reasoning
  blind1/            upstairs.yaml, downstairs.yaml, telegraf.d/
  garage/            garage.yaml, telegraf.d/
deploy/              install script, systemd units, the shared telegraf input
tests/               fixtures and tests, none of which need hardware
```

## Configuration

One file per **machine**, not per site. A site can have several machines: the
deer blind is planned as an upstairs node with the sensors and a downstairs
node with the power devices and the load control. Both write the same
measurement, so dashboards do not care how the work is divided.

`sites/example/node.yaml` documents every option and explains the ones whose
reasoning is not obvious. Start there.

Each device declares a mode:

| Mode | Runs as | For |
|---|---|---|
| `poll` | one `inputs.exec` per node | stateless reads: 1-Wire, I2C, host stats |
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
- **No secrets in this repository.** Credentials live in
  `/etc/monitoring/influx.env`, mode 600, owned by root. See
  `influx.env.example`.
- **Any condition turns a load off; all conditions must agree to turn it on.**
  The asymmetry is deliberate: state of charge drifts high and would
  under-protect alone, while voltage sags under load and would shed early,
  which is the safe direction. A condition with no fresh reading is dropped
  from the test rather than counted as false, so one stale Bluetooth value
  cannot make a restore permanently impossible.
- **Verifying what the hardware did is optional.** It can be derived from the
  measured DC load, since the switched loads differ by orders of magnitude, so
  no extra wiring is needed. A dry contact on a GPIO is available where real
  independence is wanted. Restart behaviour never depends on it: the state
  after a released pin is determined by the relay polarity.
- **Silence always means failure, never intent.** Every declared device
  publishes something every interval; one that is `enabled: false` publishes
  `enabled=0.0` and nothing else. A device this machine does not have is
  absent from the configuration instead, which is a different statement.
- **A device that cannot be read publishes that it failed**, with the reason
  as a field, rather than publishing nothing. A gap in a graph is ambiguous;
  `ok=0.0` beside an error string is not.
- **Every device is tagged with its own name**, so two devices sharing a
  location cannot write into one series and silently overwrite each other.
- **A failed write is logged, never discarded.**
- **Counters persist.** Anything that accumulates, such as a rain total or a
  lockout counter, survives a restart in a state file, because the thing it
  protects against is usually a restart.

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
  cannot see the difference.

## Running it

```
collect --node sites/garage/garage.yaml --list     what this node declares
collect --node ... --check                         probe devices, take no reading
collect --node ... --poll                          every poll-mode device, once
collect host --node ...                            one device
collect mppt --node ... --stream                   a resident device, forever
collect host --node ... --raw                      unparsed device output
```

`--poll` is what telegraf runs. Exit codes: 0 on success, 1 when a device
failed, 2 when the node file itself is wrong. A single dead sensor among
several does not mark the collection failed, because one broken sensor must
not stop a site reporting.

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

Idempotent, so updating is `git pull` and run it again. It installs telegraf
**inputs** only and never touches the output, so a machine that already writes
to InfluxDB keeps its existing configuration and its existing credentials.

Poll devices share one `exec` input, common to every machine. Resident devices
need one `execd` stanza each, committed per site under `sites/<site>/telegraf.d/`.
A test asserts that those fragments and the node file agree, because a resident
device with no stanza never runs and nothing reports it -- it simply never
appears in the data.

A device that fails its check produces a warning, not a refusal. A dead five
dollar sensor must never block a software update at a site nobody can reach.

## Continuous integration

Every push runs the suite on Python 3.9 and 3.11, the oldest and newest
versions deployed, and loads every committed node file. Linux runners only.

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
| `host` | the machine itself | none | Temperature, uptime, disk, address, processor load and available memory. Load averages rather than a percentage, because a collector that runs once and exits cannot measure a rate. |
| `ds18b20` | one-wire temperature | GPIO | Kernel one-wire bus. Addressed by sensor id, so no node file mentions which GPIO carries the bus. |
| `dht` | DHT11 and DHT22 | GPIO | Read through the kernel's industrial I/O driver rather than by bit-banging from Python, so the microsecond timing is protected from the scheduler. Needs `dtoverlay=dht11,gpiopin=N`, and the overlay is called dht11 whatever sensor is attached. |
| `rain_gauge` | tipping bucket | GPIO | Pulse counter with the total persisted across reboots. Needs the lgpio pin backend: the sysfs fallback does not release the pin, so every restart finds it busy. |
| `vedirect` | Victron MPPT | serial | VE.Direct, documented by Victron. Frames are checksum-validated and a line that delivers bytes without ever completing a frame is bounded, which an idle timer alone does not catch. |
| `pi30` | Voltronic-protocol inverters | serial | The PI30 ASCII protocol, documented by several vendors. Written and tested against recorded output, but not yet against hardware: the EG4 here does not speak it. See `eg4_modbus`. |
| `abc_bms` | SOK and other ABC-BMS packs | Bluetooth | Protocol reverse engineered for interoperability by others and reimplemented here, not copied. The published cell-voltage layout was wrong for a 16-cell rack pack, so the reader validates its own cell numbering and publishes nothing rather than a set shifted by a byte. |
| `jbd` | Chins and other JBD packs | Bluetooth | Identified as JBD from its characteristic UUIDs before anything was sent. Big-endian, and one reply arrives across several notifications, so replies are reassembled by their length byte. |
| `qinglan` | QinLan coulomb-counting shunts | Bluetooth | No published protocol and nothing findable online. Reverse engineered for interoperability from the vendor's own Android application, then confirmed against the hardware. Nothing is copied from that application: this is an independent implementation of a wire format. Every request carries a bus address, and without it the device ignores you in silence and looks dead. |
| `eg4_modbus` | EG4 all-in-one inverters | serial | Modbus RTU at 9600, not the PI30 ASCII at 2400 that the model is usually said to speak. The register map was built by changing one load at a time and watching, including disconnecting the array, which is the only way to tell solar power apart from load power while solar is carrying the load. |
| `control` | the load controller itself | none | Not a device. The control loop is its own service and writes a small document; this reads it so what it decided reaches the database through the same path as everything else. A loop that is not running fails loudly here, because every load is then sitting at its fail-safe and nothing is managing the battery. |

Every driver is read-only. Several of these battery modules accept
unauthenticated commands that switch their charge and discharge paths, and
this project sends none of them.

Two habits are worth copying if you add one. Publish nothing rather than
something wrong: each driver range-checks its fields and refuses a reading
whose own numbers contradict each other, because a wrong number in a
database outlives the session that produced it. And confirm a field against
something independent before trusting it. The inverter driver checks that
volts times amps equals watts on every read, which is what catches a
register map shifting under a firmware update, since every individual value
stays plausible when that happens.

`collect --raw <device>` prints a device's unparsed output, which is where
to start when adding hardware or confirming a field.

## Status

Telemetry: written, tested and running at two sites. Every driver in the table
above reads real hardware except `pi30`, which is written and tested against
recorded output but has not met an inverter that speaks it.

Not everything is decoded. The shunt's accumulated-totals record is unread,
and its twenty-five write commands are identified but unmapped, which is why
none of them is sent. On the inverter, the AC output current and apparent
power registers are right under load and disagree near zero, and the settings
block including the charge setpoints is unmapped. Each driver's docstring
separates what was confirmed against an independent reading from what is
inferred, which is worth reading before trusting a field.

Control: designed, not written. It is commissioned in place rather than on a
bench, with the run-time cap and the guard in force from the first live run so
that a relay which sticks on is bounded rather than unbounded.

Two preconditions are settled before then, and both are software rather than
hardware. Whether killing a process releases the GPIO line, which depends on
the gpiozero backend and is what the fail-safe story rests on. And whether two
processes can hold one input line, which decides whether the guard needs a pin
of its own.

`bin/gpio-probe` answers both on the machine that will run the control, with
the operating system it will run, and needs nothing wired:

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

Every device is read over a local link to hardware the owner owns: a serial
cable, a GPIO pin, or Bluetooth. Nothing talks to a vendor's cloud service,
and nothing needs an account.

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
