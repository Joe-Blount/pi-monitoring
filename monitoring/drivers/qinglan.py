"""Coulomb-counting battery shunts that speak the QinLan protocol.

Named after the protocol rather than the brand, for the same reason as the
other battery drivers here: the shunt, its Bluetooth module and its phone
application are sold under several names, and the protocol is the only part
that stays still.

The protocol was recovered from the vendor Android application by
reconstructing its compiled code. Nothing here is copied from that
application. The command set and the record layout were then confirmed
against real hardware, which is what the rest of this file is about.

How it works
------------
A serial-over-Bluetooth arrangement: service ``ffe0``, with ``ffe1`` used for
both writing and notifications.

A request is two bytes: a command and the device's bus address. The address
is the value the application calls the device address, and it matters. A
request carrying the wrong address is ignored in silence, which is why this
device looks dead to anyone probing it without knowing the number.

A reply echoes both bytes and then carries its payload, big-endian
throughout.

    F8  password      4 bytes
    FB  live values   24 bytes
    FC  accumulated   37 bytes
    FE  settings      30 bytes

The echoed command and address take the place of a checksum. There is none.

What is certain and what is not
-------------------------------
Every field published below was confirmed against the hardware, most of them
twice:

* The password reply returns the number set in the application, which proves
  the framing and the big-endian integer encoding together.
* Configured capacity and the shunt rating read back the values the site
  already records by other means.
* Remaining capacity falls at exactly the rate the current field implies.
  Those two readings are independent, so their agreement confirms the
  scaling of both.
* Reported state of charge matches remaining capacity divided by configured
  capacity.

The direction is bytes 20 and 21, read as one value: 1 while charging, 0
while discharging. Confirmed the same way as the magnitude. Over four
samples 29.6 seconds apart with the bank charging, remaining capacity rose
by 0.210, 0.210 and 0.209 Ah, which is 25.6, 25.6 and 25.5 A, while the
current field read 25.5 to 25.8 A. Every discharging sample reads 0 there
and loses capacity at the matching rate.

Current is published signed, positive into the battery, which is what the
charge controller and the battery management systems here already do.

The twenty-five commands that write a value are deliberately absent. They
include the factory reset, the accumulated-data clear and the zero-current
calibration, and sending one by mistake destroys the counter history this
device exists to keep.
"""

import sys

from ..duration import seconds as _seconds
from .base import Driver, DriverError, required, run_abandonable

SERVICE_UUID = "0000ffe0-0000-1000-8000-00805f9b34fb"
#: Written to and subscribed on. This device uses one characteristic for
#: both, which is why writing to its neighbours produces silence or a bare
#: error string from the Bluetooth module rather than an answer.
IO_UUID = "0000ffe1-0000-1000-8000-00805f9b34fb"

#: The lowest and highest bus address the device will accept.
MIN_ADDRESS, MAX_ADDRESS = 1, 255
DEFAULT_ADDRESS = 1

PASSWORD = 0xF8
LIVE = 0xFB
ACCUMULATED = 0xFC
SETTINGS = 0xFE

#: Every reply is a fixed size, so a reader can tell a complete frame from a
#: partial one with no terminator and no length byte to read.
FRAME_LENGTH = {PASSWORD: 4, LIVE: 24, ACCUMULATED: 37, SETTINGS: 30}

#: Commands a reading sends. Reads only. Nothing in this module writes.
READ_COMMANDS = (LIVE, SETTINGS)
IDENTITY_COMMAND = PASSWORD

#: Key under which the transport records commands that brought no reply. Not
#: a command byte: no device sends this.
UNANSWERED = "unanswered"

#: How much unrecognised traffic the reader will hold before discarding it.
#: A faulty module that chatters would otherwise grow this without limit, on
#: a machine with a few hundred megabytes of memory and nobody nearby to
#: reboot it. Several times the longest reply, so a real record is never lost
#: to the cap.
MAX_BUFFER_BYTES = 8 * max(FRAME_LENGTH.values())

#: Quiet gaps to allow before deciding a command brought no reply. Counted in
#: gaps rather than measured as one deadline, and reset whenever anything
#: arrives, so a record that takes many notifications is gathered in full
#: while a silent command still gives up quickly and leaves time for the
#: next one.
REPLY_GAPS = 3



class Field:
    """One value inside a reply frame.

    Declared rather than coded, so that correcting an offset against real
    hardware is a one-line change instead of surgery on a parser.
    """

    def __init__(self, name, command, offset, length, signed=False,
                 scale=1.0, shift=0.0, low=None, high=None):
        self.name = name
        self.command = command
        self.offset = offset
        self.length = length
        self.signed = signed
        self.scale = scale
        self.shift = shift
        self.low = low
        self.high = high

    def read(self, frame):
        raw = frame[self.offset:self.offset + self.length]
        if len(raw) != self.length:
            return None
        value = int.from_bytes(raw, "big", signed=self.signed)
        # Always a float. InfluxDB refuses a field whose type changes between
        # writes, so a field with a whole-number scale must not publish an
        # integer the first time it happens to read one.
        return float(value * self.scale + self.shift)

    def plausible(self, value):
        if value is None:
            return False
        if self.low is not None and value < self.low:
            return False
        if self.high is not None and value > self.high:
            return False
        return True


#: Bounds are deliberately wide. They exist to catch an offset that is wrong
#: by a byte, not to judge the health of a battery.
FIELDS = (
    # Magnitude. The sign comes from the direction field below, and the
    # signed value is what gets published.
    Field("amps_magnitude", LIVE, 2, 2, scale=0.01, low=0.0, high=600.0),
    Field("shunt_volts", LIVE, 4, 2, scale=0.01, low=4.0, high=120.0),
    Field("starter_volts", LIVE, 6, 2, scale=0.01, low=0.0, high=120.0),
    Field("cumulative_energy_wh", LIVE, 8, 4, scale=1.0, low=0.0, high=1e9),
    Field("remaining_capacity_ah", LIVE, 12, 4, scale=0.001, low=0.0, high=2000.0),
    # Counted up once every 59 seconds across a timed series, so minutes.
    # The application calls it running time. What it counts from is not
    # established: two separate observations put the origin at the moment
    # the current last changed direction, but that is not proven, so the
    # name says what it measures and not what it means.
    Field("running_time_minutes", LIVE, 16, 4, low=0.0, high=4.3e9),
    # 1 charging, 0 discharging. Only those two values are known, so a
    # third means the layout moved and the whole reading is refused.
    Field("charging", LIVE, 20, 2, low=0.0, high=1.0),
    Field("state_of_charge_pct", LIVE, 22, 1, low=0.0, high=100.0),

    Field("configured_capacity_ah", SETTINGS, 2, 2, scale=0.1,
          low=1.0, high=6500.0),
    Field("shunt_rating_amps", SETTINGS, 22, 2, low=1.0, high=1000.0),
)

#: How far the device's own state of charge may sit from the figure implied
#: by its remaining and configured capacity before the reading is refused.
#: The two are rounded differently and the device reports whole percent, so
#: they never match exactly. A disagreement larger than this means an offset
#: moved, which is the failure this check exists to catch.
SOC_TOLERANCE_PCT = 5.0

#: The fields the cross-check needs. If one of these is read but refused,
#: the check cannot run, and a reading that cannot be checked is not
#: published. Absent is different from refused: a command that went
#: unanswered leaves nothing to check and is reported as such.
CROSS_CHECK_FIELDS = frozenset(
    ("remaining_capacity_ah", "configured_capacity_ah", "state_of_charge_pct"))

#: Fields whose implausibility says the record is not what it was taken to
#: be, rather than one number being odd. Dropping one of these and
#: publishing the rest would leave a dashboard looking healthy with the
#: current missing, which is the quiet failure this driver exists to avoid.
#: The direction is here because only two values are known: a third means
#: the layout moved, and then nothing read from it can be trusted.
REFUSE_IF_REJECTED = frozenset(CROSS_CHECK_FIELDS | {"charging"})


def request(command, address):
    """Build one request: the command and the device's bus address.

    Only the four commands that ask for a record can be encoded. The other
    twenty-five set or clear something, and among them are the factory
    reset, the accumulated-data clear and the zero-current calibration,
    which destroy counter history that cannot be recovered. Refusing here
    means no caller can send one, whatever it passes, rather than relying on
    every caller to be careful.
    """
    if command not in FRAME_LENGTH:
        raise DriverError(
            "%#04x is not a read command, and this driver sends no others"
            % command)
    if not MIN_ADDRESS <= address <= MAX_ADDRESS:
        raise DriverError(
            "device address %r is outside %d to %d"
            % (address, MIN_ADDRESS, MAX_ADDRESS))
    return bytes([command, address])


def frame_is_valid(frame, command, address):
    """Whether a reply answers this request. Returns a reason, or None."""
    expected = FRAME_LENGTH.get(command)
    if expected is not None and len(frame) != expected:
        return "expected %d bytes, got %d" % (expected, len(frame))
    if len(frame) < 2:
        return "too short to carry a command and an address"
    if frame[0] != command:
        return "answers %#04x, not %#04x" % (frame[0], command)
    if frame[1] != address:
        return "addressed to %d, not %d" % (frame[1], address)
    return None


def take_frame(buffer, command, address):
    """Pull one valid reply out of the buffer, or return None.

    Taking the first command byte found is not enough. A command byte is an
    ordinary value that also occurs inside a payload: a current of 2.54 A is
    0x00FE, and 0xFE is the settings command. The tail of an earlier reply
    sitting in the buffer therefore offers false starts, and locking on to
    one consumes part of the real reply and loses both.

    So every candidate is validated, and one that fails is stepped past
    rather than ending the search, because the real reply may be behind it.

    Bytes before the chosen candidate are discarded. A candidate that is
    still arriving is kept, because the rest of it may yet come.

    There is no checksum in this protocol, so a payload that happens to hold
    the right command and address followed by enough bytes cannot be told
    from a reply here. That is what the plausibility bounds and the
    cross-check in parse() are for: the last defence against a wrong number
    is refusing to publish it, not framing.
    """
    expected = FRAME_LENGTH.get(command)
    if expected is None:
        return None

    start = 0
    while start < len(buffer):
        # A fast path only. Validation below rejects the same positions, so
        # removing this changes how long the scan takes and nothing else.
        if buffer[start] != command:
            start += 1
            continue
        if len(buffer) - start < expected:
            break                       # a candidate that is still arriving
        candidate = bytes(buffer[start:start + expected])
        if frame_is_valid(candidate, command, address) is None:
            del buffer[:start + expected]
            return candidate
        start += 1                      # a false start; look behind it

    del buffer[:start]
    return None


def password(frame):
    """The communication password the device reports, as written."""
    if not frame or len(frame) < FRAME_LENGTH[PASSWORD]:
        return None
    return int.from_bytes(bytes(frame[2:4]), "big")


def parse(messages):
    """Turn gathered replies into published values.

    A value outside its plausible range is dropped rather than published,
    because the commonest cause is an offset that is wrong by a byte, and a
    wrong number in the database is worse than a missing one.
    """
    out = {}
    rejected = []
    rejected_names = set()

    for field in FIELDS:
        frame = messages.get(field.command)
        if not frame:
            continue
        value = field.read(frame)
        if field.plausible(value):
            out[field.name] = value
        elif value is not None:
            rejected.append("%s=%g" % (field.name, value))
            rejected_names.add(field.name)

    # Current, signed, positive into the battery. The device reports a
    # magnitude and a direction separately, and a magnitude on its own
    # invites a dashboard to add a discharge to a charge.
    magnitude = out.pop("amps_magnitude", None)
    charging = out.get("charging")
    if magnitude is not None and charging is not None:
        # Written this way so a resting bank reads 0.0 rather than -0.0,
        # which is the same number and looks like a sign bug on a dashboard.
        out["shunt_amps"] = magnitude if charging else 0.0 - magnitude
    elif magnitude is not None:
        rejected.append("amps_magnitude=%g with no direction" % magnitude)

    # Voltage is the anchor for trusting the rest. Current cannot be
    # range-checked usefully, because a resting bank really does read near
    # zero, so a frame of noise leaves a plausible-looking current behind and
    # the reading would claim success on the strength of it. A bank that
    # answers at all has a voltage inside its own chemistry's range.
    if "shunt_volts" not in out:
        raise DriverError(
            "no plausible voltage, so nothing read is trusted%s" % (
                "; out of range: " + ", ".join(rejected) if rejected else ""))

    # Two readings of the same quantity, arriving in different records and
    # scaled differently. They agree only if both offsets are right, which
    # makes this the strongest check available without a checksum.
    remaining = out.get("remaining_capacity_ah")
    capacity = out.get("configured_capacity_ah")
    reported = out.get("state_of_charge_pct")
    if remaining is not None and capacity and reported is not None:
        implied = remaining / capacity * 100.0
        if abs(implied - reported) > SOC_TOLERANCE_PCT:
            raise DriverError(
                "state of charge %.0f%% disagrees with %.3f Ah of %.1f Ah "
                "(%.0f%%), so an offset has moved"
                % (reported, remaining, capacity, implied))
        out["implied_state_of_charge_pct"] = round(implied, 1)

    # A cross-check that was skipped proves nothing, and it is skipped
    # exactly when one of its inputs was refused. A layout shifted by a byte
    # does that: remaining capacity goes out of range, the check never runs,
    # and a plausible-looking voltage and current are published anyway. So a
    # reading that lost a cross-check input is refused whole.
    refused = REFUSE_IF_REJECTED & rejected_names
    if refused:
        raise DriverError(
            "%s out of range, so the record is not what it is taken to be "
            "and nothing read from it is trusted" % ", ".join(sorted(refused)))

    out["commands_unanswered"] = float(len(messages.get(UNANSWERED) or []))
    if rejected:
        out["rejected_fields"] = ", ".join(rejected)
    return out


class QinlanDriver(Driver):
    """A coulomb-counting battery shunt, read over Bluetooth."""

    description = "coulomb-counting battery shunt over Bluetooth (QinLan protocol)"
    needs_bluetooth = True
    # A cold Bluetooth connect alone takes 10 to 40 seconds, and each reading
    # sends two commands and waits for quiet after each.
    slow_read = True
    # Connect, read and disconnect each time rather than holding the link:
    # these modules accept one connection at a time, so a held connection
    # locks the vendor's phone application out of the shunt.
    modes = ("poll", "controller")

    def __init__(self, name, params, tags):
        Driver.__init__(self, name, params, tags)
        self.address = required(self.params, "address", name)
        # The bus address, which is part of every request. A node file that
        # omits it gets the factory value, which is what an untouched device
        # answers to.
        self.device_address = int(self.params.get("device_address",
                                                  DEFAULT_ADDRESS))
        # Durations, not bare numbers. Node files write 40s everywhere else
        # and a driver that demands a float turns a consistent file into a
        # device that cannot be built.
        self.timeout = _seconds(self.params.get("timeout", 40),
                                "device %r timeout" % name)
        self.settle = _seconds(self.params.get("settle", 2.0),
                               "device %r settle" % name)
        # Injected by the tests. Production builds one from bleak on demand,
        # so importing this module needs no Bluetooth stack.
        self.transport = self.params.get("transport")

    def _exchange(self, commands):
        transport = self.transport or _BleakTransport(
            self.address, self.device_address, self.timeout, self.settle,
            self.params.get("client_factory"))
        try:
            return transport.exchange(commands)
        finally:
            if transport is not self.transport:
                transport.close()

    def read(self):
        return parse(self._exchange(READ_COMMANDS))

    def check(self):
        messages = self._exchange((IDENTITY_COMMAND,))
        value = password(messages.get(PASSWORD))
        if value is None:
            raise DriverError(
                "%s did not answer at bus address %d. It may be out of range, "
                "another device may be holding the one connection it allows, "
                "or the bus address may differ from the one configured"
                % (self.address, self.device_address))
        # The password is reported rather than printed: it is a credential,
        # and a check that answers at all has already proved the link.
        return ("shunt %s answering at address %d, password set"
                % (self.address, self.device_address))

    def raw(self):
        """Every record the shunt returns, for confirming a field offset."""
        messages = self._exchange(
            (LIVE, ACCUMULATED, SETTINGS))
        out = {("%#04x" % key): bytes(value).hex(" ")
               for key, value in sorted(
                   (k, v) for k, v in messages.items() if isinstance(k, int))}
        unanswered = messages.get(UNANSWERED)
        if unanswered:
            out["unanswered_commands"] = ", ".join("%#04x" % c for c in unanswered)
        return out


class _BleakTransport:
    """Talks to the shunt, and knows nothing about what the bytes mean.

    Separated so the protocol above is testable with no Bluetooth stack
    installed, which is most of the value: the parsing is where the mistakes
    are, and the radio is where the slowness is.
    """

    def __init__(self, address, device_address, timeout, settle,
                 client_factory=None):
        self.address = address
        self.device_address = device_address
        self.timeout = timeout
        self.settle = settle
        # Injected by the tests, so that the exchange below is exercised as
        # written rather than replaced. Production builds a bleak client.
        self.client_factory = client_factory
        #: The most bytes ever held at once, and how often the cap had to
        #: throw traffic away. Kept because a module that chatters is a real
        #: fault on a machine with little memory, and "it grew" is the only
        #: evidence of it after the fact.
        self.peak_buffer = 0
        self.overflowed = 0

    def exchange(self, commands):
        """Run the exchange on a thread this call is willing to abandon.

        See run_abandonable: the deadline inside the exchange cannot be
        relied on, because cancelling it waits on the same system bus that
        is already wedged when it fires.
        """
        import asyncio
        return run_abandonable(
            self.address, self.timeout,
            lambda: asyncio.run(self._guarded(commands)))

    async def _guarded(self, commands):
        """Run the exchange under one hard deadline.

        Scanning, connecting, writing and waiting are all inside it. Without
        this, a radio that accepts a connection and then stops answering
        leaves the collector running forever. That is survivable under
        telegraf, which kills the process, but the installer runs a check
        with telegraf stopped and no timeout of its own, so a stall there
        leaves the whole node silent until somebody drives out to it.
        """
        import asyncio
        try:
            return await asyncio.wait_for(self._exchange(commands),
                                          timeout=self.timeout)
        except asyncio.TimeoutError:
            raise DriverError(
                "%s did not finish within %g seconds, so the attempt was "
                "given up rather than left running"
                % (self.address, self.timeout))

    def _client(self):
        if self.client_factory is not None:
            return self.client_factory(self.address, self.timeout)
        try:
            from bleak import BleakClient
        except ImportError:
            raise DriverError(
                "the qinglan driver needs bleak (apt install python3-bleak)")
        return BleakClient(self.address, timeout=self.timeout)

    async def _exchange(self, commands):
        import asyncio

        messages = {}
        unanswered = []
        arrived = asyncio.Event()
        buffer = bytearray()

        def on_notify(_sender, data):
            # A reply longer than the negotiated packet size arrives in
            # pieces, with no terminator to mark the end. The sizes are fixed
            # and known, so the reader gathers until it holds a whole record.
            buffer.extend(bytes(data))
            if len(buffer) > MAX_BUFFER_BYTES:
                self.overflowed += 1
                del buffer[:-MAX_BUFFER_BYTES]
            self.peak_buffer = max(self.peak_buffer, len(buffer))
            arrived.set()

        try:
            async with self._client() as client:
                await client.start_notify(IO_UUID, on_notify)
                for command in commands:
                    await client.write_gatt_char(
                        IO_UUID, request(command, self.device_address),
                        response=True)
                    frame = await self._reply(command, buffer, arrived)
                    reason = (frame_is_valid(frame, command,
                                             self.device_address)
                              if frame else "no reply")
                    if reason:
                        sys.stderr.write("%s: no usable reply to %#04x: %s\n"
                                         % (self.address, command, reason))
                        unanswered.append(command)
                        continue
                    messages[command] = frame
                await client.stop_notify(IO_UUID)
        except DriverError:
            raise
        except Exception as exc:
            raise DriverError("cannot read %s: %s" % (self.address, exc))

        if self.overflowed:
            sys.stderr.write(
                "%s: discarded unrecognised traffic %d time(s)\n"
                % (self.address, self.overflowed))

        if not messages:
            raise DriverError(
                "%s connected but answered nothing at bus address %d. Another "
                "device may hold the one connection it allows, or the bus "
                "address may be wrong" % (self.address, self.device_address))

        if unanswered:
            # Published as a field rather than only logged, so that a shunt
            # answering some commands and not others is visible on a dashboard
            # instead of looking healthy with fields quietly absent.
            messages[UNANSWERED] = unanswered

        return messages

    async def _reply(self, command, buffer, arrived):
        """Gather one whole record, or give up on this command.

        Waiting for a quiet gap instead was wrong twice over. A device that
        takes longer than the gap to begin answering was abandoned while its
        reply was still coming, and that reply then arrived inside the next
        command's window and spoiled a record that would otherwise have been
        good. Reading to a known length removes both, because every reply
        here is a fixed size and starts with the command it answers.
        """
        import asyncio
        quiet_gaps = 0
        while True:
            frame = take_frame(buffer, command, self.device_address)
            if frame is not None:
                return frame
            if quiet_gaps >= REPLY_GAPS:
                return None
            arrived.clear()
            try:
                await asyncio.wait_for(arrived.wait(), timeout=self.settle)
                # Something arrived, so the device is still answering. Keep
                # gathering however many pieces the record takes.
                quiet_gaps = 0
            except asyncio.TimeoutError:
                quiet_gaps += 1

    def close(self):
        """Nothing to release: the connection closes with its context."""
