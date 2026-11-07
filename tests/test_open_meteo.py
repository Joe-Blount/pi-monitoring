"""The forecast driver, with the network replaced and the clocks held still."""

import datetime
import json
import os
import urllib.error

import pytest

from monitoring import config, drivers, runner
from monitoring.control import files as control_files
from monitoring.drivers import open_meteo
from monitoring.drivers.base import DriverError, Unchanged
from tests import open_meteo_fixtures as fx

EVENING = datetime.datetime(2026, 11, 6, 20, 0, tzinfo=fx.ZONE)
HOUR = 3600.0


class Service:
    """Stands where the network would, and records what it was asked."""

    def __init__(self, reply=None):
        self.reply = fx.body() if reply is None else reply
        self.urls = []
        self.timeouts = []

    def __call__(self, url, timeout):
        self.urls.append(url)
        self.timeouts.append(timeout)
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


class World:
    """A driver's surroundings, kept across the fresh driver each poll builds."""

    def __init__(self, tmp_path, service=None):
        self.service = service or Service()
        self.mono = 1000.0
        self.now = EVENING
        self.state_file = str(tmp_path / "forecast-state.json")

    def driver(self, **params):
        params.setdefault("locate", lambda: fx.PLACE)
        params.setdefault("fetch", self.service)
        params.setdefault("monotonic", lambda: self.mono)
        params.setdefault("now", lambda zone: self.now.astimezone(zone))
        params.setdefault("state_file", self.state_file)
        return open_meteo.OpenMeteoDriver("forecast", params, {})


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


# -- the request ------------------------------------------------------------

def test_the_request_asks_for_radiation_sunrise_and_two_days_in_the_local_zone(
        world):
    world.driver().read()
    url = world.service.urls[0]
    assert url.startswith("https://api.open-meteo.com/v1/forecast?")
    assert "hourly=shortwave_radiation" in url
    assert "daily=sunrise" in url
    assert "forecast_days=2" in url
    assert "timezone=America%2FChicago" in url
    assert "cloud" not in url


def test_the_position_leaves_the_machine_rounded_to_a_tenth_of_a_degree(world):
    world.driver().read()
    url = world.service.urls[0]
    assert "latitude=12.3&" in url
    assert "longitude=-56.8&" in url
    assert "12.34" not in url
    assert "56.78" not in url


def test_a_node_file_cannot_carry_a_position_in_its_params(world):
    for key in ("latitude", "longitude"):
        with pytest.raises(DriverError) as raised:
            world.driver(**{key: 1.0})
        assert "location.yaml" in str(raised.value)


def test_the_fetch_is_given_the_timeout(world):
    world.driver(timeout="7s").read()
    assert world.service.timeouts == [7.0]


def test_the_default_timeout_fits_the_slow_collector_beside_two_packs():
    """Two packs at 40 seconds plus this, each with five seconds of grace,
    against the collector's limit of 120."""
    driver = open_meteo.OpenMeteoDriver("forecast", {}, {})
    assert 2 * (40 + 5) + driver.timeout + 5 <= 120


# -- the number -------------------------------------------------------------

def test_the_field_is_the_mean_radiation_over_the_hours_after_the_next_sunrise(
        world):
    assert world.driver().read() ["shortwave_wm2"] == 250.0


def test_the_number_of_hours_comes_from_the_params(world):
    assert world.driver(hours=2).read() ["shortwave_wm2"] == 150.0


@pytest.mark.parametrize("hours", [0, -1, 2.5, True, "four", 25])
def test_a_number_of_hours_that_makes_no_sense_is_refused(world, hours):
    with pytest.raises(DriverError):
        world.driver(hours=hours)


def test_after_the_first_sunrise_the_second_day_is_the_one_read(world):
    world.now = datetime.datetime(2026, 11, 7, 6, 59, tzinfo=fx.ZONE)
    assert world.driver().read() ["shortwave_wm2"] == 25.0


def test_before_a_sunrise_on_the_same_day_that_sunrise_is_the_one_read(world):
    world.now = datetime.datetime(2026, 11, 7, 6, 57, tzinfo=fx.ZONE)
    assert world.driver().read() ["shortwave_wm2"] == 250.0


def test_an_hour_counts_only_when_most_of_it_falls_after_sunrise(world):
    """A stamp carries the mean of the hour before it. Sunrise at 06:58 leaves
    two minutes of light in the 07:00 stamp, so that hour is not a morning
    hour, and at 07:29 the 08:00 stamp is mostly light and is."""
    document = fx.answer(first=(100.0, 200.0, 300.0, 400.0))
    assert open_meteo.mean_after_sunrise(
        document, EVENING.replace(tzinfo=None), 1) == 100.0
    document["daily"]["sunrise"][0] = "2026-11-07T07:29"
    assert open_meteo.mean_after_sunrise(
        document, EVENING.replace(tzinfo=None), 1) == 100.0
    document["daily"]["sunrise"][0] = "2026-11-07T07:31"
    assert open_meteo.mean_after_sunrise(
        document, EVENING.replace(tzinfo=None), 1) == 200.0


def test_only_the_one_field_is_published_and_nothing_from_the_answer_beside_it(
        world):
    out = world.driver().read()
    assert sorted(out) == ["shortwave_wm2", "sunrise_epoch"]
    assert isinstance(out["shortwave_wm2"], float)
    assert isinstance(out["sunrise_epoch"], float)


@pytest.mark.parametrize("damage", [
    lambda d: d.pop("hourly"),
    lambda d: d.pop("daily"),
    lambda d: d["hourly"].pop("shortwave_radiation"),
    lambda d: d["daily"].update(sunrise=[]),
    lambda d: d["daily"].update(sunrise=7),
    lambda d: d["daily"].update(sunrise=[None, None]),
    lambda d: d["daily"].update(sunrise=["not a time", "not a time"]),
    lambda d: d["hourly"].update(time=["not a time"]),
    lambda d: d["hourly"].update(time=d["hourly"]["time"][:9]),
    lambda d: d["hourly"]["shortwave_radiation"].__setitem__(8, None),
    lambda d: d["hourly"]["shortwave_radiation"].__setitem__(8, "bright"),
    lambda d: d["hourly"]["shortwave_radiation"].__setitem__(8, True),
    lambda d: d["hourly"]["shortwave_radiation"].__setitem__(8, float("nan")),
    lambda d: d["hourly"]["shortwave_radiation"].__setitem__(8, -5.0),
    lambda d: d["hourly"]["shortwave_radiation"].__setitem__(8, 50000.0),
], ids=["no hourly", "no daily", "no radiation", "no sunrise", "sunrise not a list",
        "null sunrise",
        "bad sunrise", "bad stamps", "too few hours", "null value", "text value",
        "boolean value", "not a number", "negative", "too bright"])
def test_an_answer_that_cannot_be_trusted_is_a_failed_read(tmp_path, damage):
    document = fx.answer()
    damage(document)
    world = World(tmp_path, Service(json.dumps(document).encode()))
    with pytest.raises(DriverError):
        world.driver().read()


def test_every_sunrise_in_the_past_is_a_failed_read(world):
    world.now = datetime.datetime(2026, 12, 25, 12, tzinfo=fx.ZONE)
    with pytest.raises(DriverError) as raised:
        world.driver().read()
    assert "sunrise" in str(raised.value)


@pytest.mark.parametrize("reply", [b"", b"<html>busy</html>", b"[]", b"7",
                                   b'{"error": true, "reason": "no"}',
                                   b"\xff\xfe"])
def test_a_reply_that_is_not_a_forecast_is_a_failed_read(tmp_path, reply):
    world = World(tmp_path, Service(reply))
    with pytest.raises(DriverError):
        world.driver().read()


def test_the_reply_is_read_up_to_a_limit_rather_than_without_one(monkeypatch):
    seen = {}

    class Reply:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def read(self, size=-1):
            seen["size"] = size
            return b"{}"

    def urlopen(request, timeout):
        seen["url"], seen["timeout"] = request.full_url, timeout
        return Reply()

    monkeypatch.setattr(open_meteo.urllib.request, "urlopen", urlopen)
    assert open_meteo.https_get("https://example.invalid/x", 3.0) == b"{}"
    assert seen["timeout"] == 3.0
    assert 0 < seen["size"] <= 1024 * 1024


# -- failing without giving the position away -------------------------------

PRECISE = {"latitude": 12.34, "longitude": -56.78, "zone": fx.ZONE}

LEAKS = ["12.34", "56.78", "12.3", "56.8", "latitude", "longitude"]


def assert_no_position(text):
    for leak in LEAKS:
        assert leak not in text, text


@pytest.mark.parametrize("failure", [
    urllib.error.URLError("Name or service not known"),
    urllib.error.HTTPError("https://x/?latitude=12.3&longitude=-56.8", 503,
                           "Service Unavailable", {}, None),
    TimeoutError("timed out"),
    OSError("cannot reach /v1/forecast?latitude=12.3&longitude=-56.8"),
    ValueError("https://x/?latitude=12.3&longitude=-56.8"),
], ids=["no route", "refused", "timeout", "socket", "bug"])
def test_a_failed_fetch_is_a_driver_error_that_never_quotes_the_request(
        tmp_path, failure):
    world = World(tmp_path, Service(failure))
    with pytest.raises(DriverError) as raised:
        world.driver().read()
    assert_no_position(str(raised.value))
    with open(world.state_file) as handle:
        assert_no_position(handle.read())


def test_the_status_of_a_refused_request_is_said(tmp_path):
    world = World(tmp_path, Service(urllib.error.HTTPError(
        "https://x/", 429, "Too Many Requests", {}, None)))
    with pytest.raises(DriverError) as raised:
        world.driver().read()
    assert "429" in str(raised.value)


def test_a_service_error_body_is_not_repeated(tmp_path):
    """The service's own message can quote what it was sent."""
    reply = b'{"error": true, "reason": "latitude 12.3 is not allowed"}'
    world = World(tmp_path, Service(reply))
    with pytest.raises(DriverError) as raised:
        world.driver().read()
    assert_no_position(str(raised.value))


def test_a_location_file_that_will_not_load_is_a_failed_read_that_quotes_nothing(
        world):
    def locate():
        raise ValueError("latitude of -56.78 is outside -90 to 90")
    with pytest.raises(DriverError) as raised:
        world.driver(locate=locate).read()
    assert "location" in str(raised.value)
    assert "56.78" not in str(raised.value)
    assert world.service.urls == []


def test_a_location_failure_does_not_use_up_the_hour(world):
    def locate():
        raise ValueError("no such file")
    with pytest.raises(DriverError):
        world.driver(locate=locate).read()
    assert world.driver().read() ["shortwave_wm2"] == 250.0


def test_a_driver_with_no_location_supplied_fails_only_when_it_reads(world):
    driver = open_meteo.OpenMeteoDriver(
        "forecast", {"state_file": world.state_file}, {})
    with pytest.raises(DriverError) as raised:
        driver.read()
    assert "location" in str(raised.value)


def test_a_fetch_that_hangs_past_its_timeout_is_abandoned(world, monkeypatch):
    """A socket timeout does not bound a name lookup, so the fetch gets a
    deadline of its own."""
    import threading
    release = threading.Event()
    monkeypatch.setattr("monitoring.drivers.base.JOIN_GRACE_SECONDS", 0.01)

    def hang(_url, _timeout):
        release.wait(5)
        return fx.body()

    try:
        with pytest.raises(DriverError) as raised:
            world.driver(fetch=hang, timeout=0.01).read()
    finally:
        release.set()
    assert "abandon" in str(raised.value)
    assert_no_position(str(raised.value))


# -- at most once an hour ---------------------------------------------------

def test_a_second_poll_inside_the_hour_does_not_call_the_service(world):
    world.driver().read()
    world.mono += HOUR - 1.0
    world.driver().read()
    assert len(world.service.urls) == 1


def test_the_service_is_called_again_once_the_hour_has_passed(world):
    world.driver().read()
    world.mono += HOUR
    world.driver().read()
    assert len(world.service.urls) == 2


def test_a_repeat_inside_the_hour_carries_the_value_and_says_it_is_a_repeat(
        world):
    first = world.driver().read()
    assert not isinstance(first, Unchanged)
    world.mono += 60.0
    repeat = world.driver().read()
    assert isinstance(repeat, Unchanged)
    assert repeat == first


def test_a_failed_fetch_also_waits_the_hour_and_keeps_saying_why(tmp_path):
    service = Service(urllib.error.URLError("no route"))
    world = World(tmp_path, service)
    with pytest.raises(DriverError) as first:
        world.driver().read()
    world.mono += 60.0
    with pytest.raises(DriverError) as second:
        world.driver().read()
    assert len(service.urls) == 1
    assert "cannot reach" in str(first.value)
    assert str(first.value) == str(second.value)

    service.reply = fx.body()
    world.mono += HOUR
    assert world.driver().read() ["shortwave_wm2"] == 250.0


def test_nothing_is_fetched_when_the_attempt_cannot_be_recorded(tmp_path):
    """Without the record every poll would call the service."""
    world = World(tmp_path)
    world.state_file = str(tmp_path / "no" / "such" / "directory" / "state.json")
    with pytest.raises(DriverError):
        world.driver().read()
    assert world.service.urls == []


def test_an_attempt_is_on_record_before_the_service_is_called(world):
    """A collector killed during a fetch has still called the service."""
    seen = {}

    def fetch(_url, _timeout):
        seen["recorded"] = os.path.exists(world.state_file)
        return fx.body()

    world.driver(fetch=fetch).read()
    assert seen["recorded"]


@pytest.mark.parametrize("contents", ["", "not json", "[]", '{"at": "soon"}',
                                      '{"at": true}', '{"at": 900000.0}'])
def test_a_record_that_cannot_be_believed_means_fetching(world, contents):
    with open(world.state_file, "w") as handle:
        handle.write(contents)
    assert world.driver().read() ["shortwave_wm2"] == 250.0
    assert len(world.service.urls) == 1


def test_the_record_holds_no_position_and_can_be_read_by_the_other_users(world):
    world.driver().read()
    with open(world.state_file) as handle:
        assert_no_position(handle.read())
    assert os.stat(world.state_file).st_mode & 0o044 == 0o044


def test_the_default_record_lives_where_the_control_files_live():
    driver = open_meteo.OpenMeteoDriver("forecast", {}, {})
    assert os.path.dirname(driver.state_file) == control_files.RUNTIME_DIR
    assert not driver.state_file.startswith(control_files.READINGS_DIR)


# -- the driver's place among the others ------------------------------------

def test_it_is_registered_and_read_on_the_slow_schedule():
    assert drivers.get("open_meteo") is open_meteo.OpenMeteoDriver
    assert open_meteo.OpenMeteoDriver.slow_read is True
    assert open_meteo.OpenMeteoDriver.modes == ("poll",)
    assert open_meteo.OpenMeteoDriver.needs_location is True


def test_check_calls_the_service_and_names_no_position(world):
    detail = world.driver().check()
    assert "answering" in detail
    assert_no_position(detail)
    assert len(world.service.urls) == 1


def test_check_does_not_use_up_the_hour(world):
    world.driver().check()
    assert not os.path.exists(world.state_file)


def test_check_fails_when_the_service_does_not_answer(tmp_path):
    world = World(tmp_path, Service(urllib.error.URLError("no route")))
    with pytest.raises(DriverError):
        world.driver().check()


def node_with_forecast(tmp_path, control="control: {loads: {}}"):
    path = tmp_path / "node.yaml"
    path.write_text(
        "site: s\nnode: n\nmeasurement: m\n%s\ndevices:\n"
        "  - {name: forecast, driver: open_meteo, mode: poll, "
        "params: {state_file: %s}}\n" % (control, tmp_path / "state.json"))
    return config.load(path, drivers.names())


def test_the_runner_hands_the_driver_the_machine_location_file(
        tmp_path, monkeypatch):
    place = tmp_path / "location.yaml"
    place.write_text("latitude: 12.34\nlongitude: -56.78\n"
                     "timezone: America/Chicago\n")
    monkeypatch.setenv("MONITORING_LOCATION", str(place))
    device = node_with_forecast(tmp_path).devices[0]
    driver = runner.build_driver(device)
    assert driver.url().count("latitude=12.3&") == 1


def test_a_position_the_node_file_tried_to_inject_is_replaced(tmp_path,
                                                              monkeypatch):
    place = tmp_path / "location.yaml"
    place.write_text("latitude: 12.34\nlongitude: -56.78\n"
                     "timezone: America/Chicago\n")
    monkeypatch.setenv("MONITORING_LOCATION", str(place))
    device = node_with_forecast(tmp_path).devices[0]
    device.params["locate"] = "a string in a node file"
    assert "latitude=12.3&" in runner.build_driver(device).url()


def test_building_the_driver_needs_no_location_file(tmp_path, monkeypatch):
    """Every declared device is built on a laptop by the node file tests, and
    a machine with a template location file still has to start collecting."""
    monkeypatch.setenv("MONITORING_LOCATION", str(tmp_path / "absent.yaml"))
    assert runner.build_driver(node_with_forecast(tmp_path).devices[0])


def test_a_missing_location_file_publishes_a_failure_and_does_not_crash_collection(
        tmp_path, monkeypatch):
    monkeypatch.setenv("MONITORING_LOCATION", str(tmp_path / "absent.yaml"))
    node = node_with_forecast(tmp_path)
    result = runner.read_device(node, node.devices[0], now_ns=1)
    assert not result.ok
    assert "location" in result.error


def test_a_network_error_quoting_the_url_never_reaches_the_message(tmp_path):
    secret = "latitude=12.3&longitude=-45.6"
    world = World(tmp_path, Service(urllib.error.URLError(
        "cannot open https://api.open-meteo.com/v1/forecast?" + secret)))
    with pytest.raises(DriverError) as raised:
        world.driver().read()
    assert "12.3" not in str(raised.value)
    assert "open-meteo.com" not in str(raised.value)
