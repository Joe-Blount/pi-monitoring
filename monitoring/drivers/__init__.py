"""Driver registry.

Drivers are imported lazily, by name, when a node file asks for one. That
matters on these machines: importing a GPIO library on a node that has no
GPIO devices declared costs memory on a Pi Zero and pulls in packages that
may not be installed on a laptop running the tests.
"""

import importlib

from .base import Driver, DriverError  # noqa: F401  (re-exported)

#: Driver name as written in a node file, mapped to module and class.
REGISTRY = {
    "host": ("monitoring.drivers.host", "HostDriver"),
    "dht": ("monitoring.drivers.dht", "DhtDriver"),
    "ds18b20": ("monitoring.drivers.ds18b20", "Ds18b20Driver"),
    "rain_gauge": ("monitoring.drivers.rain_gauge", "RainGaugeDriver"),
    "vedirect": ("monitoring.drivers.vedirect", "VedirectDriver"),
    "pi30": ("monitoring.drivers.pi30", "Pi30Driver"),
    "abc_bms": ("monitoring.drivers.abc_bms", "AbcBmsDriver"),
    "jbd": ("monitoring.drivers.jbd", "JbdDriver"),
    # Registered without an implementation, so that a node file naming a
    # planned device still validates. An unknown driver stays a typo rather
    # than becoming a legitimate forward reference.
    "ble_shunt": ("monitoring.drivers.stubs", "BleShuntDriver"),
}


#: Older names kept working, mapped to the name now preferred. A node file is
#: edited while standing next to the hardware it describes, so a rename that
#: breaks one is a rename that strands a machine.
ALIASES = {
    # Named after the battery brand before it was clear that the protocol
    # belongs to the BMS maker: one brand ships two unrelated protocols across
    # its range, and this protocol appears under several brands.
    "sok": "abc_bms",
    # Named after the transport before the protocol was known. It turned out
    # to be JBD, which is documented and shared with many brands.
    "ble_bms": "jbd",
}


def canonical():
    """The preferred name for each driver, with no aliases."""
    return set(REGISTRY)


def names():
    """Every driver name a node file may use, aliases included."""
    return set(REGISTRY) | set(ALIASES)


def get(name):
    """Return the driver class registered under `name`."""
    try:
        module_name, class_name = REGISTRY[ALIASES.get(name, name)]
    except KeyError:
        raise DriverError(
            "unknown driver %r; known drivers are %s"
            % (name, ", ".join(sorted(REGISTRY)))
        )
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        raise DriverError(
            "driver %r needs a package that is not installed: %s" % (name, exc)
        )
    return getattr(module, class_name)
