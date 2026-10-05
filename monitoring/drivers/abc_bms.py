"""ABC-BMS battery packs over Bluetooth low energy.

SOK and others. The protocol belongs to the BMS maker rather than to the
battery brand, so this is named after the protocol: one brand can ship two
unrelated protocols across its range, and one protocol appears under many
brands.

The protocol was reverse engineered from the ABC-BMS Android application by
others, and this is an independent implementation of what they documented
rather than a copy of their code. Two published implementations informed it:
batmon-ha (MIT) and aiobmsble (Apache-2.0). The second is the more complete,
and the resolution of the cell-voltage paging below is its contribution.

How it works
------------
A serial-over-Bluetooth arrangement: service ``ffe0``, write commands to
``ffe2``, receive notifications on ``ffe1``.

A command is six bytes: ``EE <command> 00 00 00`` followed by a CRC-8. The
replies are fixed twenty-byte frames beginning ``CC``, with a message
identifier in the second byte and a CRC-8 in the last.

    C0  identity        replies F1
    C1  status          replies F0, F2
    C2  detail          replies F0, F3, F4
    C4  protection      replies F9

Cell voltages arrive as several ``F4`` frames carrying a few cells each,
which the reader concatenates. The number of cells is then taken from the
length of what accumulated rather than assumed, which is what makes one
implementation work for a four-cell 12 V pack and a sixteen-cell 48 V rack
pack alike.

What is certain and what is not
-------------------------------
The transport, the framing, the checksum and the command set are certain:
they are consistent across both published implementations and a frame that
disagrees is rejected by its own checksum.

The field offsets are less certain. The two references disagree about where
some values sit, because they index from different places, and both were
written against small 12 V packs. So the decoding here is table-driven, to
make a correction a one-line change, and every value is range-checked before
it is published. A wrong offset then produces a loud failure rather than a
plausible-looking number in the database, which is the failure this project
cares about most.

Use ``--raw`` against a real pack to see the frames before trusting a field.
"""

import struct

from .base import Driver, DriverError, required

SERVICE_UUID = "0000ffe0-0000-1000-8000-00805f9b34fb"
NOTIFY_UUID = "0000ffe1-0000-1000-8000-00805f9b34fb"
WRITE_UUID = "0000ffe2-0000-1000-8000-00805f9b34fb"

COMMAND_HEAD = 0xEE
RESPONSE_HEAD = 0xCC
FRAME_LENGTH = 0x14          # every reply is exactly twenty bytes

#: Commands worth sending for a reading, and the messages each should bring.
READ_COMMANDS = ((0xC1, (0xF0, 0xF2)), (0xC2, (0xF0, 0xF3, 0xF4)), (0xC4, (0xF9,)))
IDENTITY_COMMAND = (0xC0, (0xF1,))

#: The message carrying cell voltages, which arrives in several parts.
CELL_MESSAGE = 0xF4


class Field:
    """One value inside a reply frame.

    Declared rather than coded so that correcting an offset against real
    hardware is a one-line change instead of surgery on a parser.
    """

    def __init__(self, name, message, offset, length, signed=False,
                 scale=1.0, low=None, high=None, little_endian=True):
        self.name = name
        self.message = message
        self.offset = offset
        self.length = length
        self.signed = signed
        self.scale = scale
        self.low = low
        self.high = high
        self.little_endian = little_endian

    def read(self, frame):
        raw = frame[self.offset:self.offset + self.length]
        if len(raw) != self.length:
            return None
        if not self.little_endian:
            raw = bytes(reversed(raw))
        value = int.from_bytes(raw, "little", signed=self.signed)
        # Always a float. InfluxDB refuses a field whose type changes between
        # writes, so a field declared with a whole-number scale must not
        # publish an integer the first time it happens to read one.
        return float(value * self.scale)

    def plausible(self, value):
        if value is None:
            return False
        if self.low is not None and value < self.low:
            return False
        if self.high is not None and value > self.high:
            return False
        return True


#: Bounds are deliberately wide: they exist to catch an offset that is wrong
#: by a byte, not to judge the health of a battery.
FIELDS = (
    Field("pack_volts", 0xF0, 2, 3, scale=0.001, low=4.0, high=120.0),
    Field("pack_amps", 0xF0, 5, 3, signed=True, scale=0.001,
          low=-600.0, high=600.0),
    Field("design_capacity_ah", 0xF0, 8, 3, scale=0.001, low=1.0, high=2000.0),
    Field("remaining_capacity_ah", 0xF0, 11, 3, scale=0.001, low=0.0, high=2000.0),
    Field("cycles", 0xF0, 14, 2, low=0.0, high=65000.0),
    Field("state_of_charge_pct", 0xF0, 16, 1, low=0.0, high=100.0),
    Field("charge_mosfet_on", 0xF2, 2, 1, low=0.0, high=1.0),
    Field("discharge_mosfet_on", 0xF2, 3, 1, low=0.0, high=1.0),
    Field("temperature_sensors", 0xF2, 4, 1, low=0.0, high=16.0),
    Field("heater_on", 0xF3, 8, 1, low=0.0, high=1.0),
)


def crc8(data):
    """The Dallas and Maxim CRC-8, which this protocol uses throughout."""
    crc = 0
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0x8C if crc & 1 else crc >> 1
    return crc


def command(code):
    """Build one command: the five byte request and its checksum."""
    frame = bytes([COMMAND_HEAD, code, 0x00, 0x00, 0x00])
    return frame + bytes([crc8(frame)])


def frame_is_valid(frame):
    """Whether a reply is well formed. Returns a reason, or None if it is."""
    if len(frame) != FRAME_LENGTH:
        return "expected %d bytes, got %d" % (FRAME_LENGTH, len(frame))
    if frame[0] != RESPONSE_HEAD:
        return "starts with %#04x, not %#04x" % (frame[0], RESPONSE_HEAD)
    if crc8(frame[:-1]) != frame[-1]:
        return "checksum does not match"
    return None


def collect(messages, frame):
    """Add one validated frame to the messages gathered so far.

    Cell voltages arrive as several frames carrying a few cells each. They are
    joined rather than replaced, which is what lets one reader handle a
    four-cell pack and a sixteen-cell one without being told which it has.
    """
    identifier = frame[1]
    if identifier == CELL_MESSAGE and CELL_MESSAGE in messages:
        messages[identifier] = messages[identifier][:-2] + frame[2:]
    else:
        messages[identifier] = bytes(frame)
    return identifier


def cell_voltages(message, entry_size=4):
    """Cell voltages from the accumulated cell message.

    The count comes from how much arrived, never from an assumption about the
    pack: the same reader must handle a four-cell 12 V pack and a sixteen-cell
    48 V rack pack.

    The exact entry layout is the one part of this protocol not confirmed
    against real hardware, so the scan validates itself rather than trusting a
    stride. Each entry is read as a cell index and a voltage, and the result
    is accepted only if the indices come out distinct, within range, and
    starting at one. Anything else returns nothing, because no cell data is
    better than cell data silently shifted by a byte.

    Use ``--raw`` against a pack and compare before relying on these.
    """
    if not message or len(message) < entry_size + 2:
        return []

    body = message[2:]
    found = {}
    for start in range(0, len(body) - entry_size + 1, entry_size):
        index = int.from_bytes(body[start:start + 2], "little")
        millivolts = int.from_bytes(body[start + 2:start + 4], "little")
        if 1 <= index <= 64 and 1000 <= millivolts <= 5000:
            found.setdefault(index, millivolts)

    if not found:
        return []

    # Self-check: a pack numbers its cells from one without gaps. Anything
    # else means the entries were not where this expected them.
    expected = set(range(1, len(found) + 1))
    if set(found) != expected:
        return []

    return [found[index] for index in sorted(found)]


def parse(messages):
    """Turn gathered messages into published values.

    A value outside its plausible range is dropped rather than published,
    because the commonest cause is an offset that is wrong by a byte, and a
    wrong number in the database is worse than a missing one.
    """
    out = {}
    rejected = []

    for field in FIELDS:
        frame = messages.get(field.message)
        if not frame:
            continue
        value = field.read(frame)
        if field.plausible(value):
            out[field.name] = value
        elif value is not None:
            rejected.append("%s=%g" % (field.name, value))

    cells = cell_voltages(messages.get(CELL_MESSAGE))
    if cells:
        out["cell_count"] = float(len(cells))
        out["cell_min_volts"] = round(min(cells) / 1000.0, 3)
        out["cell_max_volts"] = round(max(cells) / 1000.0, 3)
        out["cell_spread_volts"] = round((max(cells) - min(cells)) / 1000.0, 3)

    # Voltage is the anchor for trusting the rest. Current cannot be
    # range-checked usefully, because a resting pack really does read near
    # zero, so a frame of noise leaves a plausible-looking current behind and
    # the reading would claim success on the strength of it. A pack that
    # answers at all has a voltage inside its own chemistry's range.
    if messages.get(0xF0) and "pack_volts" not in out:
        raise DriverError(
            "frame F0 arrived but pack_volts was not plausible, so nothing in "
            "it is trusted%s" % (
                "; out of range: " + ", ".join(rejected) if rejected else ""))

    if not out:
        raise DriverError(
            "no usable values%s" % (
                "; out of range: " + ", ".join(rejected) if rejected else ""))
    if rejected:
        out["rejected_fields"] = ", ".join(rejected)
    return out


class AbcBmsDriver(Driver):
    """A pack whose BMS speaks ABC-BMS, read over Bluetooth."""

    description = "ABC-BMS battery pack over Bluetooth (SOK and others)"
    needs_bluetooth = True
    # Connect, read and disconnect each time rather than holding the link:
    # these modules accept one connection at a time, so a held connection
    # locks the vendor's phone application out of the battery.
    modes = ("poll", "controller")

    def __init__(self, name, params, tags):
        Driver.__init__(self, name, params, tags)
        self.address = required(self.params, "address", name)
        self.timeout = float(self.params.get("timeout", 40))
        self.settle = float(self.params.get("settle", 2.0))
        # Injected by the tests. Production builds one from bleak on demand,
        # so importing this module needs no Bluetooth stack.
        self.transport = self.params.get("transport")

    def _exchange(self, wanted):
        transport = self.transport or _BleakTransport(
            self.address, self.timeout, self.settle)
        try:
            return transport.exchange(wanted)
        finally:
            if transport is not self.transport:
                transport.close()

    def read(self):
        return parse(self._exchange(READ_COMMANDS))

    def check(self):
        messages = self._exchange((IDENTITY_COMMAND,))
        identity = messages.get(0xF1)
        if not identity:
            raise DriverError(
                "%s did not identify itself. It may be out of range, or "
                "another device may be holding the one connection it allows"
                % self.address)
        name = bytes(identity[2:-1]).decode("ascii", "replace").strip("\x00 ")
        return "pack %s answering%s" % (self.address, (", %s" % name) if name else "")

    def raw(self):
        """Every frame the pack returns, for confirming a field offset."""
        messages = self._exchange(READ_COMMANDS + (IDENTITY_COMMAND,))
        return {("%#04x" % key): bytes(value).hex(" ")
                for key, value in sorted(messages.items())}


class _BleakTransport:
    """Talks to the pack, and knows nothing about what the bytes mean.

    Separated so the protocol above is testable with no Bluetooth stack
    installed, which is most of the value: the parsing is where the mistakes
    are, and the radio is where the slowness is.
    """

    def __init__(self, address, timeout, settle):
        self.address = address
        self.timeout = timeout
        self.settle = settle

    def exchange(self, wanted):
        import asyncio
        return asyncio.run(self._exchange(wanted))

    async def _exchange(self, wanted):
        try:
            from bleak import BleakClient
        except ImportError:
            raise DriverError(
                "the abc_bms driver needs bleak (apt install python3-bleak)")

        import asyncio

        messages = {}
        arrived = asyncio.Event()

        def on_notify(_sender, data):
            reason = frame_is_valid(data)
            if reason:
                return          # a damaged frame is resent; a run of them times out
            collect(messages, data)
            arrived.set()

        try:
            async with BleakClient(self.address, timeout=self.timeout) as client:
                await client.start_notify(NOTIFY_UUID, on_notify)
                for code, expected in wanted:
                    await client.write_gatt_char(WRITE_UUID, command(code),
                                                 response=False)
                    # Cell voltages arrive as several frames, so waiting for
                    # the first reply is not enough. Wait for quiet instead.
                    await self._wait_for_quiet(arrived)
                    missing = [m for m in expected if m not in messages]
                    if missing and not messages:
                        raise DriverError(
                            "no reply to command %#04x from %s"
                            % (code, self.address))
                await client.stop_notify(NOTIFY_UUID)
        except DriverError:
            raise
        except Exception as exc:
            raise DriverError("cannot read %s: %s" % (self.address, exc))

        if not messages:
            raise DriverError(
                "%s connected but sent nothing. The pack may be asleep, or "
                "another device may hold the one connection it allows"
                % self.address)
        return messages

    async def _wait_for_quiet(self, arrived):
        """Wait until frames stop arriving, rather than for a fixed count."""
        import asyncio
        deadline = self.timeout
        while deadline > 0:
            arrived.clear()
            try:
                await asyncio.wait_for(arrived.wait(), timeout=self.settle)
            except asyncio.TimeoutError:
                return
            deadline -= self.settle

    def close(self):
        """Nothing to release: the connection closes with its context."""
