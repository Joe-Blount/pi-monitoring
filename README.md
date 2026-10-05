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
  control/           decision logic, persisted state, relay outputs
bin/
  collect            read devices, print line protocol
  control            the controller
  pump-guard         independent run-time limiter
sites/
  example/node.yaml  every option, with the reasoning
  blind1/            upstairs.yaml, downstairs.yaml
  garage/            garage.yaml
deploy/              install script, systemd units, telegraf fragments
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

```
sudo deploy/install.sh <site> <node>
sudo deploy/install.sh garage garage
```

Idempotent, so updating is `git pull` and run it again. It installs a telegraf
**input** only and never touches the output, so a machine that already writes
to InfluxDB keeps its existing configuration and its existing credentials.

A device that fails its check produces a warning, not a refusal. A dead five
dollar sensor must never block a software update at a site nobody can reach.

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
driver, the Victron VE.Direct driver, the DS18B20 and the rain gauge. The DHT
sensor, the EG4 inverter and the two Bluetooth devices are registered as stubs
that fail loudly rather than silently returning nothing.

Control: designed, not written, and waiting on a bench rig that must
physically demonstrate four things before any contactor is bought. That a
relay stays off through a cold boot. That killing a process driving a pin
actually drops the relay. Which gpiozero pin factory is in use. And whether
two processes can read one input line. Those results decide the safety
argument, which is currently a paragraph rather than a test.
