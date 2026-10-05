# Bringing up a node

How to install this on a machine and confirm it is working. Written to be
followed on a machine you are standing next to, with no assumption about which
site it is or what is attached to it.

Everything here is parameterised. Where you see `<site>`, `<node>` or
`<pin>`, substitute your own.

---

## Before you start

You need, on the machine itself:

- A Raspberry Pi running a current Raspberry Pi OS, with ssh working.
- A user that can `sudo`.
- Network access, at least long enough to install packages.
- A write-only InfluxDB token for the bucket this site writes to.

You need, decided in advance:

- The site and node names, which become part of the data. They are hard to
  change later, because dashboards are built on them.
- Which sensors are attached and to which pins.

---

## 1. Remote access first

Install an overlay network before anything else, not after. If the rest of the
installation goes wrong, the difference between fixing it from a chair and
driving back is this step.

```
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up --ssh
```

`tailscale up` prints a URL. Open it in a browser, on any device, and sign in.
Nothing works until you do, and it is the step people miss on a headless
machine.

Then, in the admin console, **turn off key expiry for this machine**. Node keys
expire by default, and an unattended machine will silently drop off the network
months later, at which point you cannot reach it to fix the fact that you
cannot reach it.

To reach devices that cannot run Tailscale themselves — cameras, routers,
serial converters — also advertise the local range from one machine per site:

```
printf 'net.ipv4.ip_forward = 1\nnet.ipv6.conf.all.forwarding = 1\n' | sudo tee /etc/sysctl.d/99-tailscale.conf
sudo sysctl -p /etc/sysctl.d/99-tailscale.conf
sudo tailscale up --ssh --advertise-routes=<your range>/24 --accept-routes
```

The route does nothing until you approve it in the admin console.

---

## 2. Kernel support for the sensors

Some sensors are read through the kernel rather than from Python. That is
deliberate: their protocols have microsecond timing, and a userspace program
has to win a race against the scheduler on every bit.

Add what your sensors need to `/boot/firmware/config.txt` (or `/boot/config.txt`
on older systems), then **reboot**. None of it takes effect until you do.

| Sensor | Line | Notes |
|---|---|---|
| DS18B20 one-wire temperature | `dtoverlay=w1-gpio` | Defaults to GPIO4. Add `,gpiopin=<pin>` to move it. Needs a 4.7k pull-up from data to 3V3. |
| DHT11, DHT22 or AM2302 | `dtoverlay=dht11,gpiopin=<pin>` | The overlay is called `dht11` whatever sensor you have. A DHT22 uses the same line. This is the commonest reason people think their sensor is unsupported. |
| PCF8591 or other I2C | `dtparam=i2c_arm=on` | Also add `i2c-dev` to `/etc/modules`. |

Bluetooth devices need no overlay, but they do need the stack:

```
apt-cache policy python3-bleak      # confirm it is available on this release
sudo apt install python3-bleak
```

The install script does this for you when the node file declares a Bluetooth
device, and skips it otherwise so a sensors-only machine stays small.

After the reboot, confirm the kernel found them:

```
ls /sys/bus/w1/devices/            # expect a 28-* entry per one-wire sensor
cat /sys/bus/iio/devices/iio:device0/name     # expect dht11
ls -l /dev/serial/by-id/           # expect your USB serial adapters
```

A sensor that does not appear here will not appear to this software either.
Fix it at this step rather than later: the error messages from the drivers all
point back here, but it is quicker to see it now.

---

## 3. Install

```
sudo git clone <repository> /opt/monitoring
sudo /opt/monitoring/deploy/install.sh <site> <node>
```

Run with no arguments to list the site and node names available.

The script is idempotent, so updating later is `git pull` and run it again.

What it does:

- installs `python3-yaml`
- creates `/etc/monitoring` and `/var/lib/monitoring`
- links `/etc/monitoring/node.yaml` to the node file you named
- adds the telegraf user to the `gpio`, `i2c`, `dialout` and `bluetooth` groups
- installs telegraf **inputs** only, and never touches the output, so a machine
  that already writes to InfluxDB keeps its configuration and its credentials
- removes its own previous fragments first, so a device you deleted from a node
  file does not leave a collector running for hardware that is gone
- checks each device, warns about failures, and does not refuse

It deliberately does not refuse to install when a sensor fails. A dead five
dollar sensor must never block a software update on a machine nobody can reach.

---

## 4. Credentials

If the machine does not already carry a token, install one. This keeps it off
every command line and out of shell history:

```
read -s -p "paste write token: " T; echo
T="${T//[[:space:]]/}"
{ printf 'INFLUX_HOST=%s\n'   '<your host>'
  printf 'INFLUX_ORG=%s\n'    '<your org>'
  printf 'INFLUX_BUCKET=%s\n' '<your bucket>'
  printf 'INFLUX_TOKEN=%s\n'  "$T"; } | sudo tee /etc/monitoring/influx.env >/dev/null
sudo chmod 600 /etc/monitoring/influx.env
sudo chown root:root /etc/monitoring/influx.env
unset T
```

The token needs **write** permission and nothing else. Nothing in this project
reads from InfluxDB.

Grant it on the **bucket**, by name. A token granted on something that is
actually a measurement name fails with a 403 that does not say so.

---

## 5. Confirm it works

In order. Each step tells you something the next one assumes.

**What does this node think it has?**

```
collect --node /etc/monitoring/node.yaml --list
```

Every device should appear with the mode you expect. Anything marked
`[NOT COLLECTED]` is declared but nothing will read it, which usually means a
device in controller mode on a node where control is disabled.

**Is each device actually there?**

```
collect --node /etc/monitoring/node.yaml --check
```

Probes without taking a reading. A failure here names the device and the
reason. Common ones:

| Message | Cause |
|---|---|
| `no sensor 28-… under /sys/bus/w1/devices` | Overlay missing, no reboot yet, or a mistyped id. It lists the ids actually present. |
| `no DHT sensor found` | The `dht11` overlay is missing. It prints the exact line to add. |
| `cannot open /dev/serial/by-id/…` | The adapter is unplugged, or the path contains a different cable's serial number. |
| `needs pyserial` | `sudo apt install python3-serial` |

**Does it produce data?**

```
collect --node /etc/monitoring/node.yaml --poll
```

One line of InfluxDB line protocol per device. Nothing is written anywhere;
this only prints.

**Is telegraf shipping it?**

```
systemctl is-active telegraf
journalctl -u telegraf -f
```

Watch for a minute. Write failures are logged, never discarded, so a `401` or
a `403` appears here rather than silently stopping the data.

**Is it arriving?** Query the bucket for your measurement over the last few
minutes. Every device should be present, including any you marked
`enabled: false`, which publish `enabled=0.0` so that an absence always means
failure rather than a decision somebody made months ago.

---

## 6. If control is going to run here

Only for a node that switches loads. Everything else works without any of this.

Before wiring a relay, measure what a pin actually does when software lets go.
The fail-safe design depends on a killed process releasing the line, and that
is a property of the installed GPIO library rather than of any relay.

```
sudo /opt/monitoring/bin/gpio-probe --pin <unused pin> --node /etc/monitoring/node.yaml
```

It drives the pin, kills the process holding it, and reads the pin back without
claiming the line. It refuses any pin a node file declares, that the board
reserves, or that a device tree overlay has taken.

A **PASS** means energize-to-connect is real on this machine. A **FAIL** means
a crashed controller would leave loads energized, and says how to fix it.
Unreadable is reported as unreadable, because not knowing must not look like a
pass.

Buy **active-high** relay modules. An active-low module carries its own pull-up
that beats the Pi's weak internal pull-down and energizes the relay during
every boot.

---

## 7. Adding a device later

1. Add it to the node file under `sites/<site>/<node>.yaml`.
2. If its mode is `resident`, add an `execd` stanza under
   `sites/<site>/telegraf.d/`. A test enforces that these agree, so forgetting
   it fails in CI rather than by the device quietly never reporting.
3. `git pull` on the machine and run the install script again.

To take a device out of service, set `enabled: false` rather than deleting it.
It keeps its pins and calibration, so restoring it is one word, and it keeps
publishing `enabled=0.0` so the dashboard shows it is off on purpose. Delete it
only when the hardware is genuinely gone.

---

## 8. Troubleshooting

**Data stopped and nothing says why.** Check telegraf's log first; write
failures are logged. Then run `collect --poll` by hand, which prints device
errors to standard error.

**A serial device works once and then not at all.** Only one process can hold a
serial port. If telegraf has it through a resident stanza, a manual run fails
with a confusing read error. Stop the service first.

**A device path changed by itself.** A `by-id` path contains the *cable's*
serial number, not the device's. A replacement cable changes the path.

**A sensor reads 85 C exactly.** That is a DS18B20's power-on reset value, not
a temperature. It usually means a missing or wrong pull-up resistor.

**A reading is occasionally missing.** Normal for DHT sensors, whose protocol
has no error correction beyond a checksum. The driver retries, spaced by the
sensor's own minimum sampling interval.

**The clock is wrong after a power cut.** A Pi has no real-time clock and boots
with whatever was last saved, which can be days out. Points written before time
synchronisation lands in the past.
