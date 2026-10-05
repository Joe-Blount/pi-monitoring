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
parts that are tedious to write and easy to get wrong, and getting them wrong
is how an earlier version of this system silently ignored authentication
failures for an unknown length of time.

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
| `controller` | the controller owns it | single-reader devices on a machine that also controls loads |

`controller` mode exists because some devices admit exactly one reader. A
serial port is one. Each Bluetooth connection is another. If telegraf holds the
charge controller's port, the control loop cannot read the battery, and a
control loop that cannot read its own sensor is not a control loop.

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
- Relay sequencing is tested against gpiozero's mock pin factory.

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

## Status

Telemetry: the core is written and tested, in both poll and resident mode.
Line protocol, the configuration loader, calibration, the runner, the host
driver, the Victron VE.Direct driver, the DS18B20, the rain gauge, the DHT
family, the Voltronic PI30 inverter and the ABC-BMS battery pack.

Seven of nine drivers are written. The two that remain are Bluetooth, and they
wait on knowing their protocols, which needs a scan at the site.

Two written drivers have not yet met their hardware. The PI30 inverter driver
waits on a serial adapter. The ABC-BMS driver, which reads SOK and other
packs, is complete for everything the
protocol documents with confidence, which is the transport, the framing, the
checksum, the command set and the scalar values. The layout of the cell-voltage
entries is the one part taken on trust, so that reader checks its own result
and publishes nothing rather than something shifted by a byte. Run
`collect --raw` against a pack to confirm it.

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
sudo ./bin/gpio-probe --pin 26 --node /etc/monitoring/node.yaml
```

It drives a pin, kills the process holding it, and reads the pin back without
claiming the line, which is the only way to see what was left behind. It
refuses a pin that a node file declares, that the board reserves, or that a
device tree overlay has taken -- the last of those matters because a one-wire
sensor is addressed by its id, so no node file ever mentions that GPIO4 is
carrying a bus.

## Protocols, credit and scope

Every device here is read over a local link to hardware the owner owns: a
serial cable, a GPIO pin, or Bluetooth. Nothing talks to a vendor's cloud
service, and nothing needs an account.

Every driver is read-only. Several of these battery modules accept
unauthenticated commands that can switch their charge and discharge paths, and
this project sends none of them. Switching is done with relays on circuits the
owner wired, never by asking a battery to disconnect itself.

Some of these protocols are published by the vendor, and those are the pleasant
ones. Victron documents VE.Direct, JK publishes its RS485 protocol, and EG4
sends its protocol document to anyone who asks support. Others were worked out
by people reading their own traffic, and this project reimplements what they
documented rather than copying their code. No vendor document is redistributed
here; where one exists, it is cited and left where the vendor put it.

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
