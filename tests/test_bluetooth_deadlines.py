"""Every Bluetooth read must be one a caller can walk away from.

The install script stops telegraf and then checks every device. A read that
hangs there leaves a whole node silent, at a site an hour away, with nothing
in any log saying why. Only the connect was ever bounded: enabling
notifications, each write, disabling them again and the disconnect are all
unbounded calls on the system bus.

These drive each driver's real transport against a stand-in radio, because
replacing the transport wholesale would skip the code being tested.
"""

import threading
import time

import pytest

from monitoring.drivers import abc_bms, base, jbd, qinglan
from monitoring.drivers.base import DriverError


class StallingClient:
    """A radio that accepts a connection and then never returns.

    `where` picks which call hangs. Each one is a separate unbounded system
    bus call in the real client, and a deadline that only covers the connect
    misses every one of them.

    The stall blocks rather than awaits, deliberately. An awaitable sleep is
    cancellable, so the deadline inside the exchange tidies it up and the
    thread finishes, which is not the case being guarded against. A real
    call on the system bus blocks the thread it is on and no amount of
    cancellation reaches it. That is why the backstop outside the deadline
    has to exist, so the fake has to behave the same way.
    """

    def __init__(self, address, timeout, where="write",
                 blocking=True):
        self.address = address
        self.timeout = timeout
        self.where = where
        self.blocking = blocking

    def __call__(self, address, timeout):
        return self

    async def _stall(self, name):
        import asyncio
        if self.where != name:
            return
        if self.blocking:
            time.sleep(3600)
        else:
            await asyncio.sleep(3600)

    async def __aenter__(self):
        await self._stall("connect")
        return self

    async def __aexit__(self, *_):
        await self._stall("disconnect")
        return False

    async def start_notify(self, *_a, **_k):
        await self._stall("start_notify")

    async def stop_notify(self, *_a, **_k):
        await self._stall("stop_notify")

    async def write_gatt_char(self, *_a, **_k):
        await self._stall("write")

    @property
    def services(self):
        return []


def transports(where):
    """One stalling transport per Bluetooth driver."""
    return [
        ("qinglan", qinglan._BleakTransport(
            "AA:BB:CC:DD:EE:01", 1, 0.3, 0.05,
            StallingClient("AA:BB:CC:DD:EE:01", 0.3, where)),
         (qinglan.LIVE,)),
        ("jbd", jbd._BleakTransport(
            "AA:BB:CC:DD:EE:02", 0.3, 0.05,
            StallingClient("AA:BB:CC:DD:EE:02", 0.3, where)),
         (jbd.BASIC,)),
        ("abc_bms", abc_bms._BleakTransport(
            "AA:BB:CC:DD:EE:03", 0.3, 0.05,
            StallingClient("AA:BB:CC:DD:EE:03", 0.3, where)),
         (abc_bms.READ_COMMANDS[0],)),
    ]


@pytest.fixture(autouse=True)
def no_abandoned_reads_between_tests():
    base._ABANDONED.clear()
    yield
    base._ABANDONED.clear()


@pytest.mark.parametrize(
    "where", ["connect", "start_notify", "write", "stop_notify", "disconnect"])
def test_a_stall_anywhere_in_the_exchange_is_abandoned(where, monkeypatch):
    """Watched from another thread, so losing the bound fails this test
    rather than hanging it. A test that hangs stops the suite reporting."""
    monkeypatch.setattr(base, "JOIN_GRACE_SECONDS", 0.3)

    for name, transport, commands in transports(where):
        outcome = []

        def attempt(transport=transport, commands=commands):
            try:
                transport.exchange(commands)
                outcome.append(None)
            except DriverError as exc:
                outcome.append(exc)

        started = time.monotonic()
        watcher = threading.Thread(target=attempt, daemon=True)
        watcher.start()
        watcher.join(timeout=20.0)

        assert not watcher.is_alive(), (
            "%s stalled at %s held its caller" % (name, where))
        assert time.monotonic() - started < 10.0, name
        assert outcome and outcome[0] is not None, name
        base._ABANDONED.clear()


def test_a_second_read_is_refused_while_the_first_is_still_stuck(monkeypatch):
    """A controller loop would otherwise stack one of these every tick, each
    holding a thread, an event loop and a connection the next read needs."""
    monkeypatch.setattr(base, "JOIN_GRACE_SECONDS", 0.3)
    address = "AA:BB:CC:DD:EE:04"

    def stuck_read():
        transport = jbd._BleakTransport(
            address, 0.3, 0.05,
            StallingClient(address, 0.3, "disconnect"))
        with pytest.raises(DriverError):
            transport.exchange((jbd.BASIC,))

    watcher = threading.Thread(target=stuck_read, daemon=True)
    watcher.start()
    watcher.join(timeout=20.0)
    assert not watcher.is_alive()
    assert address in base._ABANDONED

    started = time.monotonic()
    second = jbd._BleakTransport(address, 0.3, 0.05,
                                 StallingClient(address, 0.3, "write"))
    with pytest.raises(DriverError) as raised:
        second.exchange((jbd.BASIC,))
    assert "has still not returned" in str(raised.value)
    assert time.monotonic() - started < 1.0, "it waited instead of refusing"


def test_a_slow_but_responsive_device_is_cancelled_cleanly(monkeypatch):
    """The deadline inside the exchange is not redundant.

    When a device is slow but still answering the event loop, cancelling it
    works, and the read ends without leaving anything behind. Only when the
    stall blocks the thread outright does the backstop have to abandon it.
    Without the inner deadline this read would still end, but by being
    abandoned, leaking a thread and a connection that the next read needs.
    """
    monkeypatch.setattr(base, "JOIN_GRACE_SECONDS", 5.0)
    for name, transport, commands in transports("write"):
        transport.client_factory = StallingClient(
            transport.address, 0.3, "write", blocking=False)
        started = time.monotonic()
        with pytest.raises(DriverError) as raised:
            transport.exchange(commands)
        elapsed = time.monotonic() - started
        assert "did not finish within" in str(raised.value), name
        # Ended on its own deadline, well inside the backstop behind it.
        assert elapsed < 3.0, "%s took %.1fs" % (name, elapsed)
        assert base._ABANDONED == {}, (
            "%s leaked a thread that could have been cancelled" % name)
