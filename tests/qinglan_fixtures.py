"""QinLan shunt replies, both captured and generated.

The captured ones prove the driver agrees with a shunt. The generated ones
let a test ask for a case the shunt did not happen to be in: a moved offset,
a wrong address, a truncated record.
"""

from monitoring.drivers import qinglan

#: Captured from a 500 A / 50 mV shunt at bus address 1, watching a 520 Ah
#: bank discharge. Four live records taken about twenty-one seconds apart.
#: Remaining capacity falls by 0.057 Ah between each, which works out at
#: 9.77 A, and the current field reads 9.81 A. Those two readings come from
#: different offsets and different scalings, so their agreement is what
#: confirms both.
REAL_LIVE_SERIES = [
    "fb 01 03 d5 05 23 00 00 00 02 5b be 00 06 4a 0a 00 00 00 94 00 00 4f 00",
    "fb 01 03 ce 05 23 00 00 00 02 5b bf 00 06 49 d1 00 00 00 95 00 00 4f 00",
    "fb 01 03 d1 05 23 00 00 00 02 5b bf 00 06 49 98 00 00 00 95 00 00 4f 00",
    "fb 01 03 cc 05 23 00 00 00 02 5b c0 00 06 49 5f 00 00 00 95 00 00 4f 00",
]

#: The settings record from the same shunt. Configured capacity reads 520.0
#: Ah and the shunt rating reads 500 A, both of which the site already knows
#: by other means.
REAL_SETTINGS = (
    "fe 01 14 50 00 00 00 00 00 00 00 00 00 00 00 00 "
    "0a 00 00 02 bf 02 01 f4 00 00 00 00 00 00")

#: The accumulated record. Not decoded into fields yet, so it is carried here
#: only to hold its length and framing steady.
REAL_ACCUMULATED = (
    "fc 01 03 6f 05 60 00 00 00 00 03 e4 00 00 46 00 08 00 00 00 00 02 "
    "22 e5 4f 00 07 2d 61 00 07 29 ee 00 01 13 b5")

#: The password record. The value is the number the application shows, which
#: is what makes this the proof that the framing and the big-endian integer
#: encoding are both right.
REAL_PASSWORD = "f8 01 04 d2"

#: What the shunt reported at the moment of the first live record.
REAL_AMPS = 9.81
REAL_VOLTS = 13.15
REAL_REMAINING_AH = 412.170
REAL_STATE_OF_CHARGE = 79.0
REAL_CAPACITY_AH = 520.0
REAL_SHUNT_RATING_AMPS = 500.0

#: The bus address every captured record carries.
REAL_ADDRESS = 1


def frame(text):
    """One captured record, as the transport hands it to the parser."""
    return bytes.fromhex(text.replace(" ", ""))


def real_messages():
    """The captured replies, gathered as the transport gathers them."""
    return {
        qinglan.LIVE: frame(REAL_LIVE_SERIES[0]),
        qinglan.SETTINGS: frame(REAL_SETTINGS),
    }


def live_frame(amps=REAL_AMPS, volts=REAL_VOLTS, starter_volts=0.0,
               energy_wh=154558, remaining_ah=REAL_REMAINING_AH,
               counter=148, state_of_charge=79, direction=0,
               address=REAL_ADDRESS):
    """A live record built from values rather than from bytes."""
    body = bytearray(qinglan.FRAME_LENGTH[qinglan.LIVE])
    body[0] = qinglan.LIVE
    body[1] = address
    body[2:4] = int(round(amps * 100)).to_bytes(2, "big")
    body[4:6] = int(round(volts * 100)).to_bytes(2, "big")
    body[6:8] = int(round(starter_volts * 100)).to_bytes(2, "big")
    body[8:12] = int(energy_wh).to_bytes(4, "big")
    body[12:16] = int(round(remaining_ah * 1000)).to_bytes(4, "big")
    body[16:20] = int(counter).to_bytes(4, "big")
    body[22] = state_of_charge
    body[23] = direction
    return bytes(body)


def settings_frame(capacity_ah=REAL_CAPACITY_AH,
                   rating_amps=REAL_SHUNT_RATING_AMPS,
                   address=REAL_ADDRESS):
    """A settings record built from values rather than from bytes."""
    body = bytearray(qinglan.FRAME_LENGTH[qinglan.SETTINGS])
    body[0] = qinglan.SETTINGS
    body[1] = address
    body[2:4] = int(round(capacity_ah * 10)).to_bytes(2, "big")
    body[22:24] = int(round(rating_amps)).to_bytes(2, "big")
    return bytes(body)


class FakeClient:
    """Stands in for a bleak client, so the real exchange runs as written.

    A transport replaced wholesale proves nothing about the bytes that reach
    a radio. This replaces only the radio, which leaves the request encoding,
    the reassembly and the giving-up in the code under test.
    """

    #: Every write the exchange performed, in order, exactly as sent.
    def __init__(self, address, timeout, replies=None, chunk=20, delay=0.0,
                 chunk_delay=0.0, noise=b"", silent=False, hang=False,
                 hang_on_exit=False, hang_on_write=False,
                 delays=None):
        self.address = address
        self.timeout = timeout
        self.replies = dict(replies or {})
        self.chunk = chunk
        self.delay = delay
        self.chunk_delay = chunk_delay
        self.noise = noise
        self.silent = silent
        self.hang = hang
        self.hang_on_exit = hang_on_exit
        self.hang_on_write = hang_on_write
        # Per-command delivery delay, which is what lets a test put one
        # command's reply inside another command's window.
        self.delays = dict(delays or {})
        self.writes = []
        self.notify = None
        self.entered = False
        self.exited = False

    def __call__(self, address, timeout):
        return self

    async def __aenter__(self):
        import asyncio
        if self.hang:
            await asyncio.sleep(3600)
        self.entered = True
        return self

    async def __aexit__(self, *_):
        import asyncio
        if self.hang_on_exit:
            # Tearing down a real connection waits on the system bus, which
            # is the thing already wedged when a deadline fires. Cancelling
            # the exchange therefore waits here, past its own deadline.
            await asyncio.sleep(3600)
        self.exited = True
        return False

    async def start_notify(self, _uuid, callback):
        self.notify = callback

    async def stop_notify(self, _uuid):
        self.notify = None

    async def write_gatt_char(self, _uuid, data, response=True):
        import asyncio
        self.writes.append(bytes(data))
        if self.hang_on_write:
            await asyncio.sleep(3600)
        if self.silent:
            return
        reply = self.replies.get(bytes(data)[0])
        if reply is None:
            return
        payload = self.noise + reply
        wait = self.delays.get(bytes(data)[0], self.delay)
        if wait or self.chunk_delay:
            asyncio.get_event_loop().create_task(self._deliver(payload, wait))
        else:
            self._push(payload)

    async def _deliver(self, payload, wait=0.0):
        import asyncio
        if wait:
            await asyncio.sleep(wait)
        if not self.chunk_delay:
            self._push(payload)
            return
        for start in range(0, len(payload), self.chunk):
            self.notify(None, payload[start:start + self.chunk])
            await asyncio.sleep(self.chunk_delay)

    def _push(self, payload):
        for start in range(0, len(payload), self.chunk):
            self.notify(None, payload[start:start + self.chunk])


class FakeTransport:
    """Hands back prepared replies, with no Bluetooth stack involved."""

    def __init__(self, messages):
        self.messages = dict(messages)
        self.asked = []
        self.closed = False

    def exchange(self, commands):
        self.asked.extend(commands)
        out = {c: self.messages[c] for c in commands if c in self.messages}
        missing = [c for c in commands if c not in self.messages]
        if missing:
            out[qinglan.UNANSWERED] = missing
        return out

    def close(self):
        self.closed = True
