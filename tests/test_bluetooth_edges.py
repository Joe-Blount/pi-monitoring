"""What a Bluetooth read keeps when the radio misbehaves at its edges."""

import asyncio

import pytest

from monitoring.drivers import jbd
from monitoring.drivers.base import connected
from tests import jbd_fixtures as fx


class AnsweringClient:
    """Answers each jbd register with its frame, then fails to disconnect
    the way the system bus library does when the disconnect loses its reply."""

    FRAMES = {jbd.BASIC: fx.basic_frame,
              jbd.CELLS: lambda: fx.cells_frame(fx.REAL_CELL_MILLIVOLTS),
              jbd.HARDWARE: fx.hardware_frame}

    def __init__(self, address, timeout, ignore_first=0):
        self.notify = None
        self.disconnected = False
        self.ignore_first = ignore_first
        self.writes = []

    def __call__(self, address, timeout):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        self.disconnected = True
        raise EOFError()

    async def start_notify(self, _uuid, callback):
        self.notify = callback

    async def stop_notify(self, _uuid):
        pass

    async def write_gatt_char(self, _uuid, data, response=False):
        register = data[2]
        self.writes.append(register)
        if len(self.writes) <= self.ignore_first:
            return
        self.notify(None, bytearray(self.FRAMES[register]()))


def test_a_complete_read_survives_a_failed_disconnect():
    client = AnsweringClient("AA:BB:CC:DD:EE:FF", 5.0)
    transport = jbd._BleakTransport("AA:BB:CC:DD:EE:FF", 5.0, 0.05, client)
    messages = transport.exchange((jbd.BASIC, jbd.CELLS, jbd.HARDWARE))
    assert {jbd.BASIC, jbd.CELLS, jbd.HARDWARE} <= set(messages)
    assert client.disconnected


def test_a_failure_inside_the_connection_still_propagates():
    client = AnsweringClient("AA:BB:CC:DD:EE:FF", 5.0)

    async def body():
        async with connected(client):
            raise ValueError("no reply")

    with pytest.raises(ValueError):
        asyncio.run(body())
    assert client.disconnected


def test_a_register_the_pack_lets_go_by_is_asked_again():
    client = AnsweringClient("AA:BB:CC:DD:EE:FF", 5.0, ignore_first=1)
    transport = jbd._BleakTransport("AA:BB:CC:DD:EE:FF", 5.0, 0.05, client)
    messages = transport.exchange((jbd.BASIC, jbd.CELLS, jbd.HARDWARE))
    assert {jbd.BASIC, jbd.CELLS, jbd.HARDWARE} <= set(messages)
    assert client.writes == [jbd.BASIC, jbd.CELLS, jbd.HARDWARE, jbd.BASIC]
