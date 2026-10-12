"""Report the health of the machine doing the monitoring.

Deliberately the first driver written. It needs no wiring, so it proves the
whole chain end to end -- node file, runner, line protocol, telegraf, InfluxDB
-- before any sensor is involved. When something later goes wrong, this is
also the driver that says whether the machine itself is the problem.

Every path it reads is injectable, so the whole thing is testable on a laptop
against a fixture tree rather than only on Linux.
"""

import os
import socket

from .base import Driver, DriverError


#: What the memory readings are divided by to reach megabytes. The kernel
#: reports kibibytes, so this is 1024 rather than 1000, and the figures then
#: match what free(1) prints.
KIB_PER_MIB = 1024.0


class HostDriver(Driver):
    """Temperature, load, memory, uptime, boot time, disk and address."""

    description = "machine health: temperature, load, memory, uptime, disk"

    def __init__(self, name, params, tags):
        Driver.__init__(self, name, params, tags)
        # Pointing these at a fixture tree is what makes this testable off a
        # Raspberry Pi. In production they are the real paths.
        self.sysfs_root = self.params.get("sysfs_root", "/")
        self.filesystem = self.params.get("filesystem", "/")
        self.report_address = self.params.get("report_address", True)

    # -- helpers ----------------------------------------------------------

    def _path(self, relative):
        return os.path.join(self.sysfs_root, relative.lstrip("/"))

    def _first_line(self, relative):
        try:
            with open(self._path(relative)) as handle:
                return handle.readline().strip()
        except OSError:
            return None

    # -- individual readings ----------------------------------------------

    def cpu_temp_f(self):
        """Chip temperature in Fahrenheit.

        Fahrenheit because every temperature in this project is Fahrenheit,
        converted in the driver. This is the chip, not the air around it, and
        it is the one that throttles.
        """
        raw = self._first_line("sys/class/thermal/thermal_zone0/temp")
        try:
            return round(int(raw) / 1000.0 * 9.0 / 5.0 + 32.0, 1)
        except (TypeError, ValueError):
            return None

    def load(self):
        """The kernel's load averages over one, five and fifteen minutes.

        Load rather than a processor percentage, because a collector that
        runs once and exits cannot measure a percentage: that needs two
        samples separated by time. Load is already an average the kernel
        keeps, so one read is honest where a computed percentage would be a
        guess dressed up as a measurement.

        Three windows rather than one, because the shape is the diagnosis. A
        one minute figure well above the fifteen is something starting now;
        the reverse is something finishing.
        """
        raw = self._first_line("proc/loadavg")
        try:
            one, five, fifteen = (float(v) for v in raw.split()[:3])
        except (AttributeError, IndexError, ValueError):
            return None, None, None
        return one, five, fifteen

    def cpu_count(self):
        """Processors the kernel can see.

        Read from the fixture tree rather than asked of the running machine,
        so a test can describe a four core Pi while running on a laptop.
        """
        try:
            with open(self._path("proc/cpuinfo")) as handle:
                count = sum(1 for line in handle
                            if line.startswith("processor"))
        except OSError:
            return None
        return float(count) if count else None

    def meminfo(self):
        """The kernel's memory report, as a mapping of kibibytes."""
        out = {}
        try:
            with open(self._path("proc/meminfo")) as handle:
                for line in handle:
                    name, _, rest = line.partition(":")
                    try:
                        out[name.strip()] = float(rest.split()[0])
                    except (IndexError, ValueError):
                        continue
        except OSError:
            return {}
        return out

    def memory(self):
        """Available memory and how much of swap is in use.

        Available rather than free, deliberately. Free memory on a machine
        with a page cache reads alarmingly low and means nothing: the kernel
        hands most of it back the moment something asks. MemAvailable is the
        kernel's own estimate of what a new process could actually get, and
        it is the number that would have predicted this project's one real
        memory failure, where free sat near a hundred megabytes while
        available was more than twice that.

        Swap is reported because on a machine that boots from a memory card,
        swapping is both a symptom of pressure and a cause of the slowness
        that follows it. Swap in use is an early warning where available
        memory is the late one.

        A kernel too old to report MemAvailable gets no availability figure
        rather than a different quantity published under the same name.
        """
        info = self.meminfo()
        if not info:
            return None, None, None

        total = info.get("MemTotal")
        available = info.get("MemAvailable")
        available_mb = used_pct = None
        if total and available is not None:
            available_mb = round(available / KIB_PER_MIB, 1)
            used_pct = round(100.0 * (total - available) / total, 1)

        swap_total = info.get("SwapTotal")
        swap_free = info.get("SwapFree")
        swap_pct = None
        if swap_total and swap_free is not None:
            swap_pct = round(100.0 * (swap_total - swap_free) / swap_total, 1)

        return available_mb, used_pct, swap_pct

    def boot_time(self):
        """Epoch seconds of the last boot, so a reboot shows as a step."""
        try:
            with open(self._path("proc/stat")) as handle:
                for line in handle:
                    if line.startswith("btime"):
                        return float(line.split()[1])
        except (OSError, IndexError, ValueError):
            pass
        return None

    def uptime_seconds(self):
        """Seconds since boot. Graphs as a sawtooth, so resets stand out."""
        raw = self._first_line("proc/uptime")
        try:
            return round(float(raw.split()[0]), 1)
        except (AttributeError, IndexError, ValueError):
            return None

    def root_filesystem(self):
        """Free gigabytes and percent used, as insurance against a full card.

        Percent used is computed the way df does it, against the space a
        normal user can actually reach. That excludes the filesystem's
        reserved blocks; dividing by the raw total instead reads several
        points higher than df and makes the two disagree for no good reason.
        """
        try:
            stat = os.statvfs(self.filesystem)
        except OSError:
            return None, None
        free = stat.f_bavail * stat.f_frsize
        used = (stat.f_blocks - stat.f_bfree) * stat.f_frsize
        if used + free <= 0:
            return None, None
        return round(free / 1e9, 2), round(100.0 * used / (used + free), 1)

    def address(self):
        """The address of whichever interface holds the default route.

        Opening a datagram socket sends nothing. It only makes the kernel
        choose a source address, which is the one a reply would come back to.
        Published so the machine can be reached without hunting for it, which
        matters when a lease changes at a site an hour away.
        """
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.settimeout(1)
                sock.connect(("8.8.8.8", 80))
                return sock.getsockname()[0]
        except OSError:
            return None

    # -- driver interface -------------------------------------------------

    def read(self):
        free_gb, used_pct = self.root_filesystem()
        one, five, fifteen = self.load()
        processors = self.cpu_count()
        mem_available_mb, mem_used_pct, swap_used_pct = self.memory()
        fields = {
            "cpu_temp_f": self.cpu_temp_f(),
            "boot_time": self.boot_time(),
            "uptime_seconds": self.uptime_seconds(),
            "root_free_gb": free_gb,
            "root_used_pct": used_pct,
            "load_1m": one,
            "load_5m": five,
            "load_15m": fifteen,
            "cpu_count": processors,
            # Load divided by processors, so one means saturated whatever the
            # machine is. Without it a reading of 2.0 is healthy on the
            # four core Pi and desperate on a single core one, and a
            # dashboard cannot hold both thresholds at once.
            "load_per_core": (round(one / processors, 3)
                              if one is not None and processors else None),
            "mem_available_mb": mem_available_mb,
            "mem_used_pct": mem_used_pct,
            "swap_used_pct": swap_used_pct,
        }
        if self.report_address:
            fields["ip"] = self.address()

        if not any(value is not None for value in fields.values()):
            raise DriverError(
                "no host readings available under %r" % self.sysfs_root
            )
        return fields

    def check(self):
        missing = [
            name for name, relative in (
                ("uptime", "proc/uptime"),
                ("boot time", "proc/stat"),
            ) if self._first_line(relative) is None
        ]
        if missing:
            raise DriverError(
                "cannot read %s under %r" % (" or ".join(missing), self.sysfs_root)
            )
        temp = self.cpu_temp_f()
        one, _, _ = self.load()
        _, mem_used_pct, _ = self.memory()
        return "uptime readable, temperature %s, load %s, memory %s" % (
            "%.1f F" % temp if temp is not None else "not exposed",
            "%.2f" % one if one is not None else "not exposed",
            "%.0f%% used" % mem_used_pct if mem_used_pct is not None
            else "not exposed",
        )
