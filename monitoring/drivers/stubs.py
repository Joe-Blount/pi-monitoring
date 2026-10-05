"""Drivers that are declared but not yet written.

They exist so that a node file naming a planned device still validates, which
keeps the "unknown driver is an error at load" rule useful for catching typos.
Each one fails loudly when asked to read, never silently returns nothing.

The docstrings carry what is already known about each device, so that whoever
implements one is not starting from a blank file.
"""

from .base import Driver, DriverError


class NotWrittenYet(Driver):
    device = "device"

    def read(self):
        raise DriverError(
            "the %s driver is declared but not implemented yet" % type(self).device)

    def check(self):
        raise DriverError(
            "the %s driver is declared but not implemented yet" % type(self).device)


class DhtDriver(NotWrittenYet):
    """DHT11 or DHT22 temperature and humidity on one GPIO pin.

    A bit-banged protocol with microsecond timing, so single reads fail often
    on a busy machine and retries are normal rather than exceptional. Reports
    Celsius natively; this project publishes Fahrenheit, converted here.
    """
    device = "dht"


class BleBmsDriver(NotWrittenYet):
    """Battery management system over Bluetooth low energy.

    Connect, read, disconnect rather than holding the link: these modules
    accept one connection at a time, so a held connection locks the vendor
    phone app out of the battery.

    A cold connect routinely takes 10 to 40 seconds, which is long enough to
    look like a hang, so this must never run inside a watchdog-fed loop.
    """
    device = "ble_bms"


class BleShuntDriver(NotWrittenYet):
    """Battery shunt over Bluetooth low energy.

    Same connection discipline as the battery management system. The sign of
    the current depends on which way the shunt was wired, so it is a
    configuration value and must be verified against reality before anything
    derived from it is trusted.
    """
    device = "ble_shunt"
