"""DHT11, DHT22 and AM2302 temperature and humidity sensors.

Read through the kernel's own driver rather than by bit-banging the pin from
Python. The device tree overlay exposes the sensor as an industrial I/O device,
so taking a reading is reading a file, exactly as it is for a one-wire
temperature sensor.

That choice matters more than it looks. These sensors speak a protocol with
microsecond timing, and a userspace program doing it has to win a race against
the scheduler on every single bit. Losing that race is what produces the "high
edge value duration exceeded" failures seen on a busy machine. Inside the
kernel the timing is protected, and the pure-Python alternatives all come with
their own problem: the long-standing Adafruit library is dead upstream and no
longer builds cleanly, and its replacement wants pip on systems that refuse
system-wide installs.

Enable it with one line in ``/boot/firmware/config.txt`` and a reboot:

    dtoverlay=dht11,gpiopin=17

The overlay is named ``dht11`` whatever sensor is attached. A DHT22 and an
AM2302 use the same line. That is a naming quirk rather than a mistake, and it
is the single most common reason people conclude their DHT22 is unsupported.

The kernel exposes thousandths of a degree Celsius and thousandths of a
percent of relative humidity.

These sensors fail a reading often and by design: the protocol has no error
correction beyond a checksum, and the kernel reports a failed checksum as an
ordinary read error. Retrying is normal rather than exceptional, and the
sensor's own minimum sampling interval governs how fast it is worth trying.
"""

import glob
import os
import time

from .base import Driver, DriverError

IIO_ROOT = "/sys/bus/iio/devices"
DRIVER_NAME = "dht11"

#: The sensor cannot be sampled faster than this. A DHT22 converts once every
#: two seconds; asking sooner returns the previous answer or an error.
MINIMUM_INTERVAL = 2.0


class DhtDriver(Driver):
    """Temperature and humidity from a DHT-family sensor."""

    description = "DHT11, DHT22 or AM2302 temperature and humidity"

    def __init__(self, name, params, tags):
        Driver.__init__(self, name, params, tags)
        self.model = str(self.params.get("model", "DHT22")).upper()
        self.pin = self.params.get("pin")
        self.iio_root = self.params.get("iio_root", IIO_ROOT)
        self.device = self.params.get("iio_device")
        self.retries = int(self.params.get("retries", 3))
        self.retry_wait = float(self.params.get("retry_wait", MINIMUM_INTERVAL))
        # These sensors occasionally return a reading that is well formed and
        # plainly wrong. The checksum cannot catch that, so an implausible
        # value is retried rather than published.
        self.max_humidity = float(self.params.get("max_humidity", 100.0))
        self.max_temp_f = float(self.params.get("max_temp_f", 160.0))
        self.min_temp_f = float(self.params.get("min_temp_f", -40.0))

    # -- locating the sensor ----------------------------------------------

    def _candidates(self):
        found = []
        for path in sorted(glob.glob(os.path.join(self.iio_root, "iio:device*"))):
            try:
                with open(os.path.join(path, "name")) as handle:
                    if handle.read().strip() == DRIVER_NAME:
                        found.append(path)
            except OSError:
                continue
        return found

    def _device_path(self):
        if self.device:
            path = self.device
            if not os.path.isabs(path):
                path = os.path.join(self.iio_root, path)
            return path

        found = self._candidates()
        if not found:
            raise DriverError(
                "no DHT sensor found under %s. Add this to the boot "
                "configuration and reboot:  dtoverlay=dht11,gpiopin=%s  "
                "(the overlay is called dht11 whatever sensor is attached, "
                "a DHT22 included)"
                % (self.iio_root, self.pin if self.pin is not None else "<pin>"))
        if len(found) > 1 and not self.device:
            raise DriverError(
                "%d DHT sensors are present (%s) and nothing says which one "
                "this is. Set iio_device on the device to choose."
                % (len(found), ", ".join(os.path.basename(p) for p in found)))
        return found[0]

    # -- one attempt -------------------------------------------------------

    def _scaled(self, path, filename):
        """Read one value, which the kernel reports in thousandths."""
        full = os.path.join(path, filename)
        try:
            with open(full) as handle:
                return int(handle.read().strip()) / 1000.0
        except FileNotFoundError:
            raise DriverError("%s does not expose %s" % (path, filename))
        except OSError as exc:
            # The usual case rather than the exceptional one: the kernel
            # reports a failed checksum or a sensor that did not answer as an
            # ordinary read error.
            raise DriverError("the sensor did not answer (%s)" % exc.strerror)
        except ValueError:
            raise DriverError("%s returned something that is not a number" % filename)

    def _attempt(self, path):
        celsius = self._scaled(path, "in_temp_input")
        humidity = self._scaled(path, "in_humidityrelative_input")
        fahrenheit = round(celsius * 9.0 / 5.0 + 32.0, 1)

        if not 0.0 <= humidity <= self.max_humidity:
            raise DriverError("humidity of %.1f%% is not plausible" % humidity)
        if not self.min_temp_f <= fahrenheit <= self.max_temp_f:
            raise DriverError("temperature of %.1f F is not plausible" % fahrenheit)

        return {"temp": fahrenheit, "humidity": round(humidity, 1)}

    # -- driver interface --------------------------------------------------

    def read(self):
        """Take a reading, retrying because a single failure is routine.

        Attempts are spaced by the sensor's own minimum sampling interval.
        Trying again immediately returns the same stale answer or the same
        error, so it would waste the retry without improving the odds.
        """
        path = self._device_path()
        failures = []
        for attempt in range(self.retries + 1):
            if attempt:
                time.sleep(self.retry_wait)
            try:
                return self._attempt(path)
            except DriverError as exc:
                failures.append(str(exc))

        raise DriverError(
            "%d attempts all failed: %s. A sensor that never answers is "
            "usually wiring: check the data line, the pull-up, and that "
            "gpiopin in the overlay matches where it is actually connected"
            % (len(failures), "; ".join(dict.fromkeys(failures))))

    def check(self):
        path = self._device_path()
        try:
            values = self._attempt(path)
        except DriverError:
            # Being present matters more than one reading succeeding, since
            # a single failure is routine for this sensor.
            return "%s present at %s, first reading failed (not unusual)" % (
                self.model, os.path.basename(path))
        return "%s at %s reading %.1f F, %.1f%%" % (
            self.model, os.path.basename(path), values["temp"], values["humidity"])

    def raw(self):
        path = self._device_path()
        out = {"device": path}
        for filename in ("name", "in_temp_input", "in_humidityrelative_input"):
            try:
                with open(os.path.join(path, filename)) as handle:
                    out[filename] = handle.read().strip()
            except OSError as exc:
                out[filename] = "error: %s" % exc.strerror
        return out
