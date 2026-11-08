"""A small web page to force this node's loads on or off.

The page is a second door to the switches file, not a second controller. It
reads what the loop published and what the collectors cached, and it changes
only the switches, through monitoring/control/override.py, exactly as
`control override` and `control shedding` do. It can do nothing the command
line cannot.

It runs in a process of its own, started by systemd when a connection comes
in and gone two minutes after the last request, so a fault here cannot stop
the loop and the page costs no memory between uses.

It has no login. Only a client on the tailnet or the site's own network gets
an answer, which is the same gate SSH on these machines has. A POST must also
come from the page itself, so another web site open in the same phone browser
cannot send a force.
"""

import datetime
import html
import http.server
import ipaddress
import math
import os
import socket
import time
import urllib.parse

from monitoring import config, duration
from monitoring import drivers as driver_registry
from monitoring.control import files, location, override, spec

#: The durations the page offers. The page refuses any other, so what it can
#: set stays as small as what it shows.
DURATIONS = ("5m", "15m", "1h", "4h", "12h")

#: How long the process waits for a request before it exits.
IDLE_SECONDS = 120.0

#: A reading of the panel voltage, shown at the top beside the charge.
PANEL_FIELD = "pv_volts"

STYLE = """
body{font-family:system-ui,sans-serif;margin:0 auto;max-width:40rem;
padding:1rem;background:#fff;color:#111}
@media (prefers-color-scheme:dark){body{background:#111;color:#eee}
.load{border-color:#444}}
h1{font-size:1.3rem}.warn{background:#c62828;color:#fff;padding:.6rem}
.error{background:#ef6c00;color:#fff;padding:.6rem}
.load{border:1px solid #ccc;border-radius:.4rem;padding:.6rem;margin:.6rem 0}
.on{color:#2e7d32;font-weight:bold}.off{color:#c62828;font-weight:bold}
button,select,input{font-size:1rem;padding:.4rem;margin:.2rem 0}
.muted{opacity:.7;font-size:.9rem}
"""


class Paths:
    """Where the page reads and writes. Each one can be moved for a test."""

    def __init__(self, node, switches=None, published=None, readings=None,
                 boot=None):
        self.node = node
        self.switches = switches
        self.published = published or files.PUBLISHED
        self.readings = readings
        self.boot = boot


#: The client ranges the page answers: loopback, the private ranges of a
#: site network, link-local, and the tailnet. Named one by one, because a
#: test such as "not global" also passes ranges that reach the internet
#: through a relay.
ALLOWED_NETWORKS = tuple(ipaddress.ip_network(net) for net in (
    "127.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",
    "169.254.0.0/16", "100.64.0.0/10", "::1/128", "fc00::/7", "fe80::/10"))

#: How long one connection may take to send its request.
REQUEST_SECONDS = 10


def _address(text):
    try:
        ip = ipaddress.ip_address(text.split("%", 1)[0])
    except ValueError:
        return None
    if ip.version == 6 and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip


def allowed_client(address):
    """Whether a client address may use the page."""
    ip = _address(address)
    return ip is not None and any(ip in net for net in ALLOWED_NETWORKS)


def allowed_host(host):
    """Whether the Host a browser sent names this machine.

    An address the page answers, this machine's own name, or a tailnet name.
    Any other name can be a site that points its name at this machine after
    the browser has loaded it, and then its own page would pass the check
    that a change comes from the same host.
    """
    if not host:
        return False
    name = urllib.parse.urlsplit("//" + host).hostname or ""
    if allowed_client(name):
        return True
    own = socket.gethostname().lower().split(".")[0]
    return (name in (own, own + ".local", "localhost")
            or name.endswith(".ts.net"))


def _age_text(seconds):
    if seconds is None or not math.isfinite(seconds):
        return "age unknown"
    if seconds < 90:
        return "%d s old" % seconds
    if seconds < 5400:
        return "%d min old" % (seconds // 60)
    return "%.1f h old" % (seconds / 3600)


def _local(epoch, zone):
    if zone is None:
        return "at an hour this machine cannot place"
    return datetime.datetime.fromtimestamp(epoch, zone).strftime(
        "%Y-%m-%d %H:%M")


class View:
    """Everything one page shows, read fresh for each request."""

    def __init__(self, paths):
        self.paths = paths
        self.problems = []
        self.node = None
        self.loads = {}
        try:
            self.node = config.load(paths.node,
                                    known_drivers=driver_registry.names())
            self.loads = spec.load_control(self.node)
        except (config.ConfigError, spec.SpecError) as exc:
            self.problems.append("The node file cannot be read: %s" % exc)
        try:
            self.zone = location.read()["zone"]
        except location.LocationError:
            self.zone = None
        self.switches = files.read_switches(paths.switches)
        self.readings = files.read_readings(paths.readings,
                                            this_boot=paths.boot)
        self.published, self.published_age = self._published()

    def _published(self):
        record = files.read_json(self.paths.published)
        if not record:
            return {}, None
        boot = self.paths.boot or files.boot_id()
        stamp = record.get("monotonic")
        if (not boot or record.get("boot_id") != boot
                or isinstance(stamp, bool)
                or not isinstance(stamp, (int, float))):
            return record.get("loads") or {}, None
        return (record.get("loads") or {},
                max(0.0, files.monotonic() - float(stamp)))

    def stale_after(self):
        interval = 30.0
        if self.node is not None:
            try:
                interval = duration.seconds(
                    self.node.control.get("loop_interval", 30))
            except duration.DurationError:
                pass
        return 3 * interval + 30

    def headline(self):
        """The readings the loads decide on, and the panel voltage."""
        wanted = []
        raw = (self.node.control.get("loads") or {}) if self.node else {}
        for name in sorted(self.loads):
            block = raw.get(name) or {}
            # A fan names its source at the top; a shed load names it in
            # its ladder.
            for source in (block.get("source"),
                           (block.get("shed_ladder") or {}).get("source")):
                if isinstance(source, dict) and source.get("device"):
                    pair = (source["device"], source.get("field"))
                    if pair not in wanted:
                        wanted.append(pair)
        for device in sorted(self.readings):
            if PANEL_FIELD in self.readings[device]["fields"]:
                wanted.append((device, PANEL_FIELD))
        out = []
        for device, field in wanted:
            reading = self.readings.get(device)
            value = (reading or {}).get("fields", {}).get(field)
            out.append((device, field, value,
                        reading["age_seconds"] if reading else None))
        return out


def render(view, message=None):
    esc = html.escape
    parts = ["<!doctype html><html lang=en><head><meta charset=utf-8>",
             "<meta name=viewport content='width=device-width,"
             "initial-scale=1'><title>Load overrides</title>",
             "<style>%s</style></head><body>" % STYLE]
    title = "Load overrides"
    if view.node is not None:
        title = "%s %s: load overrides" % (view.node.site, view.node.node)
    parts.append("<h1>%s</h1>" % esc(title))
    if message:
        parts.append("<p class=error>%s</p>" % esc(message))
    for problem in view.problems:
        parts.append("<p class=error>%s</p>" % esc(problem))

    if view.published_age is None or view.published_age > view.stale_after():
        parts.append(
            "<p class=warn>The controller has not published recently. It is "
            "possibly not running. A force has no effect until it runs, and "
            "every load stays at its fail-safe.</p>")
    else:
        parts.append("<p class=muted>Controller published %s.</p>"
                     % _age_text(view.published_age))

    for device, field, value, age in view.headline():
        shown = "no reading" if value is None else "%g" % value
        parts.append("<div>%s %s: <b>%s</b> <span class=muted>(%s)</span>"
                     "</div>" % (esc(device), esc(field), esc(shown),
                                 _age_text(age)))

    shedding = view.switches["shedding_enabled"]
    parts.append(
        "<form method=post action=/shedding><p>Shedding is <b>%s</b>. "
        "<button name=state value=%s>Turn shedding %s</button></p></form>"
        % ("on" if shedding else "off", "off" if shedding else "on",
           "off" if shedding else "on"))

    options = "".join("<option>%s</option>" % d for d in DURATIONS)
    now = time.time()
    for name in sorted(view.loads):
        published = view.published.get(name) or {}
        powered = published.get("powered")
        state = ("unknown" if powered is None
                 else "on" if powered else "off")
        parts.append("<div class=load><b>%s</b>: <span class=%s>%s</span>"
                     % (esc(name), state, state))
        if published.get("reason"):
            parts.append("<div class=muted>%s</div>"
                         % esc(str(published["reason"])))
        entry = view.switches["loads"].get(name)
        # Clear is offered for every stored force, past its expiry too. A
        # controller whose clock it does not trust can still hold one.
        if isinstance(entry, dict) and entry.get("force") in override.STATES:
            expires = entry.get("expires_epoch")
            if isinstance(expires, bool) or not isinstance(
                    expires, (int, float)):
                when = "with no readable expiry"
            elif expires > now:
                when = "until %s" % _local(expires, view.zone)
            else:
                when = "expired %s" % _local(expires, view.zone)
            parts.append(
                "<form method=post action=/clear><input type=hidden name=load"
                " value='%s'>Forced <b>%s</b> %s "
                "<button>Clear</button></form>"
                % (esc(name, quote=True), esc(entry["force"]), esc(when)))
        parts.append(
            "<form method=post action=/force><input type=hidden name=load "
            "value='%s'><select name=for>%s<option value=until>until</option>"
            "</select> <input type=datetime-local name=until> "
            "<button name=state value=on>Force on</button> "
            "<button name=state value=off>Force off</button></form></div>"
            % (esc(name, quote=True), options))
    if view.zone is None:
        parts.append("<p class=muted>The location file cannot be read, so a "
                     "local time cannot be placed.</p>")
    parts.append("</body></html>")
    return "".join(parts)


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "control-page"
    paths = None
    # Server is one request at a time, so a client that connects and sends
    # nothing must not hold it.
    timeout = REQUEST_SECONDS

    def log_message(self, form, *args):
        pass

    def _refuse_client(self):
        if not allowed_client(self.client_address[0]):
            self._send(403, "text/plain", "This page answers only the "
                                          "tailnet and the site network.\n")
            return True
        if not allowed_host(self.headers.get("Host")):
            self._send(403, "text/plain", "Open this page by the machine's "
                                          "address or tailnet name.\n")
            return True
        return False

    def _send(self, status, kind, body):
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", kind + "; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Frame-Options", "DENY")
        self.end_headers()
        self.wfile.write(data)

    def _page(self, status=200, message=None):
        self._send(status, "text/html", render(View(self.paths), message))

    def do_GET(self):
        if self._refuse_client():
            return
        if urllib.parse.urlsplit(self.path).path != "/":
            self._send(404, "text/plain", "not found\n")
            return
        self._page()

    def _same_origin(self):
        host = self.headers.get("Host")
        source = self.headers.get("Origin") or self.headers.get("Referer")
        if not host or not source:
            return False
        return urllib.parse.urlsplit(source).netloc == host

    def do_POST(self):
        if self._refuse_client():
            return
        if not self._same_origin():
            self._send(403, "text/plain", "A change must come from this "
                                          "page.\n")
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        form = urllib.parse.parse_qs(
            self.rfile.read(min(max(length, 0), 4096)).decode(
                "utf-8", "replace"))

        def field(name):
            return (form.get(name) or [""])[0]

        action = urllib.parse.urlsplit(self.path).path
        try:
            self._apply(action, field)
        except override.OverrideError as exc:
            self._page(400, str(exc))
            return
        except LookupError:
            self._send(404, "text/plain", "not found\n")
            return
        self.send_response(303)
        self.send_header("Location", "/")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _apply(self, action, field):
        path = self.paths.switches
        if action == "/shedding":
            if field("state") not in override.STATES:
                raise override.OverrideError("shedding is 'on' or 'off'")
            override.set_shedding(field("state") == "on", path=path)
            return
        view = View(self.paths)
        if action == "/clear":
            if field("load") not in view.loads:
                raise override.OverrideError(
                    "%r is not a load on this node" % field("load"))
            override.clear(field("load"), path=path)
            return
        if action != "/force":
            raise LookupError(action)
        choice = field("for")
        if choice == "until":
            if view.zone is None:
                raise override.OverrideError(
                    "the location file cannot be read, so a local time "
                    "cannot be placed; choose a duration")
            expires = override.expiry_until(field("until"), view.zone)
            if expires <= time.time():
                raise override.OverrideError("that time has already passed")
        elif choice in DURATIONS:
            expires = override.expiry_for(choice, time.time())
        else:
            raise override.OverrideError(
                "choose one of %s, or until" % ", ".join(DURATIONS))
        override.force(field("load"), field("state"), expires, path=path,
                       known=set(view.loads))


class Server(http.server.HTTPServer):
    """One request at a time, and an exit after a quiet spell."""

    def __init__(self, address, handler, listening=None):
        self.idle = False
        if listening is not None:
            super().__init__(address, handler, bind_and_activate=False)
            self.socket = listening
            self.server_address = listening.getsockname()
        else:
            super().__init__(address, handler)
        self.timeout = IDLE_SECONDS

    def handle_timeout(self):
        self.idle = True

    def serve_until_idle(self):
        while not self.idle:
            self.handle_request()


def handler_for(paths):
    return type("BoundHandler", (Handler,), {"paths": paths})


def inherited_socket():
    """The listening socket systemd passed, or None when started by hand."""
    if os.environ.get("LISTEN_PID") != str(os.getpid()):
        return None
    if int(os.environ.get("LISTEN_FDS") or 0) < 1:
        return None
    return socket.socket(fileno=3)
