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
    # Declared but not implemented yet. They are registered so that a node
    # file naming a planned device still validates, which keeps an unknown
    # driver a typo rather than a legitimate forward reference.
    "dht": ("monitoring.drivers.dht", "DhtDriver"),
    "ds18b20": ("monitoring.drivers.ds18b20", "Ds18b20Driver"),
    "rain_gauge": ("monitoring.drivers.rain_gauge", "RainGaugeDriver"),
    "vedirect": ("monitoring.drivers.vedirect", "VedirectDriver"),
    "pi30": ("monitoring.drivers.pi30", "Pi30Driver"),
    "ble_bms": ("monitoring.drivers.stubs", "BleBmsDriver"),
    "ble_shunt": ("monitoring.drivers.stubs", "BleShuntDriver"),
}


def names():
    """Every driver name a node file may use."""
    return set(REGISTRY)


def get(name):
    """Return the driver class registered under `name`."""
    try:
        module_name, class_name = REGISTRY[name]
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
