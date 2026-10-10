"""JBD battery management systems over Bluetooth low energy.

Sold as JBD, Jiabaida, Overkill Solar, Xiaoxiang and under a long list of
battery brands including Chins. Named after the protocol rather than any of
them, because the protocol is the thing that is the same.

Unlike the ABC-BMS packs, this protocol is published: Overkill Solar hosts the
specification openly and several independent implementations agree on it. So
the offsets here are documented rather than inferred, and were confirmed
against a pack on first contact.

How it works
------------
Serial over Bluetooth again, but a different arrangement: service ``ff00``,
write commands to ``ff02``, receive notifications on ``ff01``.

A command is seven bytes::

    DD A5 <register> 00 <checksum, 2 bytes> 77

A reply has the same shape with a status byte and a payload::

    DD <register> <status> <length> <payload...> <checksum, 2 bytes> 77

Everything is BIG endian, which is the opposite of the ABC-BMS packs and the
easiest mistake to make when working on both.

Three registers are worth reading:

    0x03  basic information: voltage, current, capacity, cycles, state of
          charge, protection flags, switch states, temperatures
    0x04  cell voltages, two bytes each
    0x05  a hardware identification string

Reassembly
----------
A reply is not one notification. The basic information reply arrives as three,
because it is longer than a Bluetooth low energy packet. The length byte says
how much to expect, so frames are accumulated until complete rather than
assumed to arrive whole. A reader that treats each notification as a frame
sees a valid first fragment and silently loses two thirds of the fields.
"""

import struct
import sys

from ..duration import seconds as _seconds
from .base import Driver, DriverError, required

SERVICE_UUID = "0000ff00-0000-1000-8000-00805f9b34fb"
WRITE_UUID = "0000ff02-0000-1000-8000-00805f9b34fb"
NOTIFY_UUID = "0000ff01-0000-1000-8000-00805f9b34fb"

START = 0xDD
END = 0x77
REQUEST = 0xA5

#: Registers worth reading, and what each is called in the output.
BASIC, CELLS, HARDWARE = 0x03, 0x04, 0x05
READ_REGISTERS = (BASIC, CELLS, HARDWARE)

#: Everything before the payload: start, register, status, length.
HEADER = 4
#: Everything after it: two checksum bytes and the end marker.
TRAILER = 3

#: A pack numbers its cells from one and has at least this many. Fewer is a
#: misread rather than a battery.
MIN_CELLS = 2
MAX_CELLS = 32

#: Kelvin, in tenths, is how temperatures arrive. Water freezes at 2731.
KELVIN_TENTHS_AT_ZERO_C = 2731

#: Bounds exist to catch an offset that moved, not to judge a battery.
LIMITS = {
    "pack_volts": (0.5, 120.0),
    "pack_amps": (-600.0, 600.0),
    "remaining_capacity_ah": (0.0, 2000.0),
    "design_capacity_ah": (0.1, 2000.0),
    "cycles": (0.0, 65000.0),
    "state_of_charge_pct": (0.0, 100.0),
}


def checksum(body):
    """The two byte checksum JBD puts before the end marker.

    The sum of the length byte and the payload, negated, as an unsigned
    sixteen bit value.
    """
    return (0x10000 - sum(body)) & 0xFFFF


def command(register):
    """Build a read request for one register."""
    body = bytes([register, 0x00])
    return bytes([START, REQUEST]) + body + struct.pack(">H", checksum(body)) + bytes([END])


def frames(buffer):
    """Pull every complete reply out of a buffer, leaving the remainder.

    Returns a list of frames and whatever bytes are left over.

    This is where a naive reader goes wrong. A reply longer than one Bluetooth
    packet arrives in pieces, and each piece looks like data. The length byte
    is what says when a frame is finished, so it is believed rather than the
    packet boundary.
    """
    out = []
    data = bytes(buffer)

    while True:
        start = data.find(bytes([START]))
        if start < 0:
            return out, b""
        data = data[start:]
        if len(data) < HEADER:
            return out, data

        length = data[3]
        total = HEADER + length + TRAILER
        if len(data) < total:
            return out, data                 # wait for the rest

        frame, data = data[:total], data[total:]
        if frame[-1] == END:
            out.append(frame)
        else:
            # Not a frame after all. Step over the false start marker rather
            # than discarding everything after it.
            data = frame[1:] + data


def frame_is_valid(frame):
    """Why a reply cannot be trusted, or None if it can."""
    if len(frame) < HEADER + TRAILER:
        return "only %d bytes" % len(frame)
    if frame[0] != START or frame[-1] != END:
        return "not delimited by %#04x and %#04x" % (START, END)
    if frame[2] != 0x00:
        return "the pack reported status %#04x" % frame[2]

    length = frame[3]
    if len(frame) != HEADER + length + TRAILER:
        return "says %d payload bytes but carries %d" % (
            length, len(frame) - HEADER - TRAILER)

    body = frame[3:HEADER + length]
    if struct.unpack(">H", frame[-3:-1])[0] != checksum(body):
        return "checksum does not match"
    return None


def payload(frame):
    """The payload of a validated frame."""
    return frame[HEADER:HEADER + frame[3]]


def _signed(body, offset):
    return struct.unpack_from(">h", body, offset)[0]


def _unsigned(body, offset):
    return struct.unpack_from(">H", body, offset)[0]


def parse_basic(body):
    """Register 0x03. Everything scalar about the pack."""
    if len(body) < 23:
        raise DriverError("basic information is %d bytes, too short" % len(body))

    out = {
        "pack_volts": _unsigned(body, 0) / 100.0,
        "pack_amps": _signed(body, 2) / 100.0,
        "remaining_capacity_ah": _unsigned(body, 4) / 100.0,
        "design_capacity_ah": _unsigned(body, 6) / 100.0,
        "cycles": float(_unsigned(body, 8)),
        "protection_flags": float(_unsigned(body, 16)),
        # Which cells the management system is actively balancing, as a
        # bitfield across two sixteen bit words. Zero means none. Worth having
        # on a site whose balance problems have already cost it batteries.
        "balancing_flags": float((_unsigned(body, 14) << 16) | _unsigned(body, 12)),
        "state_of_charge_pct": float(body[19]),
        # Bit 0 is the charge switch, bit 1 the discharge switch.
        "charge_mosfet_on": float(body[20] & 0x01),
        "discharge_mosfet_on": float(bool(body[20] & 0x02)),
        "cell_count": float(body[21]),
        "temperature_sensors": float(body[22]),
    }

    # Temperatures follow the sensor count, in tenths of a Kelvin. The count
    # says how many, so a pack with three reports three.
    for index in range(body[22]):
        at = 23 + index * 2
        if at + 2 > len(body):
            break
        celsius = (_unsigned(body, at) - KELVIN_TENTHS_AT_ZERO_C) / 10.0
        if -40.0 <= celsius <= 90.0:
            out["temp_%d" % (index + 1)] = round(celsius * 9.0 / 5.0 + 32.0, 1)

    rejected = []
    for name, (low, high) in LIMITS.items():
        if name in out and not low <= out[name] <= high:
            rejected.append("%s=%g" % (name, out.pop(name)))
    if rejected:
        out["rejected_fields"] = ", ".join(rejected)

    # Voltage anchors the rest, exactly as it does for the other battery
    # driver: current reads near zero on a resting pack, so a frame of noise
    # would otherwise leave a plausible-looking value behind.
    if "pack_volts" not in out:
        raise DriverError(
            "no plausible pack voltage, so nothing in this frame is trusted%s"
            % ("; " + out["rejected_fields"] if rejected else ""))
    return out


def cell_voltages(body):
    """Register 0x04. Two bytes per cell, big endian, in millivolts.

    No index accompanies a cell here, unlike the ABC-BMS packs, so position is
    the only thing identifying one. That makes the length the whole check: an
    odd number of bytes, or an implausible count, means this is not what it
    claims to be.
    """
    if len(body) < MIN_CELLS * 2 or len(body) % 2:
        return []

    count = len(body) // 2
    if not MIN_CELLS <= count <= MAX_CELLS:
        return []

    cells = [_unsigned(body, i * 2) for i in range(count)]
    if not all(1000 <= mv <= 5000 for mv in cells):
        return []
    return cells


def parse(messages):
    """Turn the gathered replies into published values."""
    if BASIC not in messages:
        raise DriverError(
            "the pack sent no basic information%s"
            % (", only %s" % ", ".join("%#04x" % r for r in sorted(messages))
               if messages else ""))

    out = parse_basic(messages[BASIC])

    cells = cell_voltages(messages.get(CELLS, b""))
    if cells:
        out["cell_count"] = float(len(cells))
        out["cell_min_volts"] = round(min(cells) / 1000.0, 3)
        out["cell_max_volts"] = round(max(cells) / 1000.0, 3)
        out["cell_spread_volts"] = round((max(cells) - min(cells)) / 1000.0, 3)
        for number, millivolts in enumerate(cells, start=1):
            out["cell_%02d_volts" % number] = round(millivolts / 1000.0, 3)

    out["commands_unanswered"] = float(
        len([r for r in READ_REGISTERS if r not in messages]))
    return out


def hardware_name(body):
    """Register 0x05, an identification string."""
    return bytes(body).decode("ascii", "replace").strip("\x00 ")


class JbdDriver(Driver):
    """A pack whose management system speaks JBD, read over Bluetooth."""

    description = "JBD battery management system over Bluetooth (Chins and others)"
    needs_bluetooth = True
    slow_read = True
    # Connect, read, disconnect. These accept one connection at a time, so a
    # held link locks the owner out of the vendor application.
    modes = ("poll", "controller")

    def __init__(self, name, params, tags):
        Driver.__init__(self, name, params, tags)
        self.address = required(self.params, "address", name)
        # Durations, not bare numbers. Node files write 40s everywhere else
        # and a driver that demands a float turns a consistent file into a
        # device that cannot be built.
        self.timeout = _seconds(self.params.get("timeout", 40),
                                "device %r timeout" % name)
        self.settle = _seconds(self.params.get("settle", 2.0),
                               "device %r settle" % name)
        self.transport = self.params.get("transport")

    def _exchange(self, registers):
        transport = self.transport or _BleakTransport(
            self.address, self.timeout, self.settle)
        try:
            return transport.exchange(registers)
        finally:
            if transport is not self.transport:
                transport.close()

    def read(self):
        return parse(self._exchange(READ_REGISTERS))

    def check(self):
        messages = self._exchange((HARDWARE,))
        body = messages.get(HARDWARE)
        if not body:
            raise DriverError(
                "%s did not identify itself. It may be out of range, or "
                "another device may hold the one connection it allows"
                % self.address)
        return "pack %s answering, %s" % (self.address, hardware_name(body))

    def raw(self):
        """Every reply, for confirming a field against the vendor display."""
        messages = self._exchange(READ_REGISTERS)
        return {("%#04x" % key): bytes(value).hex(" ")
                for key, value in sorted(messages.items())}


class _BleakTransport:
    """Talks to the pack and knows nothing about what the bytes mean."""

    def __init__(self, address, timeout, settle):
        self.address = address
        self.timeout = timeout
        self.settle = settle

    def exchange(self, registers):
        import asyncio
        return asyncio.run(self._exchange(registers))

    async def _exchange(self, registers):
        try:
            from bleak import BleakClient
        except ImportError:
            raise DriverError("the jbd driver needs bleak (apt install python3-bleak)")

        import asyncio

        messages = {}
        buffer = bytearray()
        arrived = asyncio.Event()

        def on_notify(_sender, data):
            # Appended rather than parsed, because one reply spans several
            # notifications and only the length byte says where it ends.
            buffer.extend(data)
            complete, rest = frames(buffer)
            buffer[:] = rest
            for frame in complete:
                if frame_is_valid(frame) is None:
                    messages[frame[1]] = payload(frame)
                    arrived.set()

        unanswered = []
        try:
            async with BleakClient(self.address, timeout=self.timeout) as client:
                await client.start_notify(NOTIFY_UUID, on_notify)
                for register in registers:
                    before = set(messages)
                    await client.write_gatt_char(
                        WRITE_UUID, command(register), response=False)
                    await self._wait_for_quiet(arrived)
                    if not set(messages) - before:
                        unanswered.append(register)
                await client.stop_notify(NOTIFY_UUID)
        except DriverError:
            raise
        except Exception as exc:                  # noqa: BLE001
            raise DriverError("cannot read %s: %s" % (self.address, exc))

        if not messages:
            raise DriverError(
                "%s connected but sent nothing usable. The pack may be "
                "asleep, or another device may hold the one connection it "
                "allows" % self.address)

        if unanswered:
            sys.stderr.write(
                "%s did not answer register(s) %s\n"
                % (self.address, ", ".join("%#04x" % r for r in unanswered)))
        return messages

    async def _wait_for_quiet(self, arrived):
        """Wait until replies stop arriving rather than for a fixed count."""
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
