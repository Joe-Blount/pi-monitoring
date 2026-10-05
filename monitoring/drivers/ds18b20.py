"""DS18B20 one-wire temperature sensor.

The kernel driver exposes each sensor as a directory under
``/sys/bus/w1/devices`` named by the sensor's own id, which always begins
``28-``. Addressing by that id rather than by position on the bus is what
makes a reading survive another sensor being added to the same wire.

Needs ``dtoverlay=w1-gpio`` in the boot configuration and a 4.7k pull-up from
the data line to 3V3. Without the pull-up the directory never appears, which
looks exactly like a dead sensor.

The file it produces looks like this:

    a2 01 4b 46 7f ff 0c 10 d8 : crc=d8 YES
    a2 01 4b 46 7f ff 0c 10 d8 t=26125

The first line ends in YES or NO, which is the sensor's own check on the bytes
it sent. The second carries the temperature in thousandths of a degree
Celsius.
"""

import glob
import os

from .base import Driver, DriverError, required

#: The value a DS18B20 holds in its register before its first conversion. A
#: reading of exactly this is almost never a real temperature: it means the
#: sensor was read before it finished converting, or that it is not getting
#: enough power, which usually means a missing or wrong pull-up resistor.
POWER_ON_RESET_MILLICELSIUS = 85000


class Ds18b20Driver(Driver):
    """Read one DS18B20 by its one-wire id."""

    description = "DS18B20 one-wire temperature"

    def __init__(self, name, params, tags):
        Driver.__init__(self, name, params, tags)
        self.device_id = required(self.params, "device_id", name)
        self.field = self.params.get("field", "temp")
        # Injectable so the whole driver is testable against a fixture tree
        # rather than only on a machine with a sensor attached.
        self.bus_root = self.params.get(
            "bus_root", "/sys/bus/w1/devices")
        # A reading of exactly 85 C is the sensor's reset value. Treating it
        # as real has fooled people into chasing a heat problem that was a
        # wiring problem, so it is rejected by default.
        self.reject_reset_value = self.params.get("reject_reset_value", True)

    @property
    def path(self):
        return os.path.join(self.bus_root, self.device_id, "w1_slave")

    def _contents(self):
        try:
            with open(self.path) as handle:
                return handle.read()
        except FileNotFoundError:
            available = sorted(
                os.path.basename(p) for p in glob.glob(os.path.join(self.bus_root, "28-*"))
            )
            raise DriverError(
                "no sensor %s under %s%s"
                % (self.device_id, self.bus_root,
                   ("; present: " + ", ".join(available)) if available
                   else "; none present, check dtoverlay=w1-gpio and the 4.7k pull-up"))
        except OSError as exc:
            raise DriverError("cannot read %s: %s" % (self.path, exc))

    def _millicelsius(self, text):
        lines = text.strip().splitlines()
        if len(lines) < 2:
            raise DriverError("%s gave an unreadable response" % self.device_id)

        if not lines[0].rstrip().endswith("YES"):
            # The sensor's own check failed, so the bytes are not trustworthy.
            # The kernel retries internally; a failure here is usually a long
            # cable, a missing pull-up, or electrical noise.
            raise DriverError("%s failed its own checksum" % self.device_id)

        marker = lines[1].rfind("t=")
        if marker < 0:
            raise DriverError("%s reported no temperature" % self.device_id)
        try:
            return int(lines[1][marker + 2:])
        except ValueError:
            raise DriverError("%s reported an unreadable temperature" % self.device_id)

    def read(self):
        millicelsius = self._millicelsius(self._contents())

        if self.reject_reset_value and millicelsius == POWER_ON_RESET_MILLICELSIUS:
            raise DriverError(
                "%s returned exactly 85 C, its power-on reset value. That is "
                "almost always a power or pull-up problem rather than a real "
                "temperature. Set reject_reset_value: false if this site can "
                "genuinely reach 185 F." % self.device_id)

        celsius = millicelsius / 1000.0
        return {self.field: round(celsius * 9.0 / 5.0 + 32.0, 1)}

    def check(self):
        self._millicelsius(self._contents())
        return "sensor %s present and answering" % self.device_id

    def raw(self):
        return self._contents()
