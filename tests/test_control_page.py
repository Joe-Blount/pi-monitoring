"""The override page, served for real on a loopback port."""

import http.client
import json
import threading
import time
import urllib.parse

import pytest

from monitoring.control import files, page
from tests.test_control_cli import BOOT, controlled_node, location_file


@pytest.fixture
def served(tmp_path, monkeypatch):
    monkeypatch.setenv("MONITORING_LOCATION", str(location_file(tmp_path)))
    paths = page.Paths(str(controlled_node(tmp_path)),
                       switches=str(tmp_path / "switches.json"),
                       published=str(tmp_path / "control.json"),
                       readings=str(tmp_path / "readings"), boot=BOOT)
    server = page.Server(("127.0.0.1", 0), page.handler_for(paths))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server, paths
    server.shutdown()
    server.server_close()


def request(server, method, path, form=None, origin=True):
    host, port = server.server_address[:2]
    connection = http.client.HTTPConnection(host, port, timeout=5)
    headers = {}
    body = None
    if form is not None:
        body = urllib.parse.urlencode(form)
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    if origin is True:
        headers["Origin"] = "http://%s:%d" % (host, port)
    elif origin:
        headers["Origin"] = origin
    connection.request(method, path, body=body, headers=headers)
    response = connection.getresponse()
    text = response.read().decode()
    connection.close()
    return response.status, text


def publish(paths, powered=1.0, reason="all tiers clear"):
    files.write_json(paths.published, {
        "fields": {"passes": 1.0}, "boot_id": BOOT,
        "monotonic": files.monotonic(),
        "loads": {"cameras": {"powered": powered, "reason": reason}}})


def switches(paths):
    with open(paths.switches) as handle:
        return json.load(handle)


def test_the_page_shows_each_load_with_its_published_state(served):
    server, paths = served
    publish(paths, reason="soc=50.0 above every threshold")
    status, text = request(server, "GET", "/")
    assert status == 200
    assert "cameras" in text and "above every threshold" in text
    assert "has not published recently" not in text


def test_the_page_warns_when_the_controller_has_published_nothing(served):
    server, _ = served
    status, text = request(server, "GET", "/")
    assert status == 200
    assert "has not published recently" in text


def test_the_page_shows_the_reading_a_load_decides_on(served):
    server, paths = served
    files.publish_reading({"state_of_charge_pct": 42.0, "pv_volts": 18.5},
                          "bms", directory=paths.readings, boot=BOOT)
    _, text = request(server, "GET", "/")
    assert "state_of_charge_pct: <b>42</b>" in text
    assert "pv_volts: <b>18.5</b>" in text


def test_a_force_from_the_page_is_written_with_its_expiry(served):
    server, paths = served
    before = time.time()
    status, _ = request(server, "POST", "/force",
                        {"load": "cameras", "state": "on", "for": "5m"})
    assert status == 303
    entry = switches(paths)["loads"]["cameras"]
    assert entry["force"] == "on"
    assert before + 290 < entry["expires_epoch"] < time.time() + 310
    _, text = request(server, "GET", "/")
    assert "Forced <b>on</b>" in text


def test_a_force_until_a_local_time_is_written(served):
    server, paths = served
    status, _ = request(server, "POST", "/force",
                        {"load": "cameras", "state": "off", "for": "until",
                         "until": "2099-01-01T12:00"})
    assert status == 303
    assert switches(paths)["loads"]["cameras"]["force"] == "off"


def test_a_force_until_a_time_already_past_is_refused(served):
    server, paths = served
    status, text = request(server, "POST", "/force",
                           {"load": "cameras", "state": "on", "for": "until",
                            "until": "2000-01-01T12:00"})
    assert status == 400 and "already passed" in text


def test_clear_from_the_page_removes_the_force(served):
    server, paths = served
    request(server, "POST", "/force",
            {"load": "cameras", "state": "on", "for": "1h"})
    status, _ = request(server, "POST", "/clear", {"load": "cameras"})
    assert status == 303
    assert switches(paths)["loads"] == {}


def test_shedding_from_the_page_sets_the_switch(served):
    server, paths = served
    status, _ = request(server, "POST", "/shedding", {"state": "off"})
    assert status == 303
    assert switches(paths)["shedding_enabled"] is False


def test_a_post_from_another_site_is_refused(served):
    server, paths = served
    status, _ = request(server, "POST", "/force",
                        {"load": "cameras", "state": "on", "for": "1h"},
                        origin="http://elsewhere.example")
    assert status == 403
    assert files.read_switches(paths.switches)["loads"] == {}


def test_a_post_with_no_origin_is_refused(served):
    server, paths = served
    status, _ = request(server, "POST", "/shedding", {"state": "off"},
                        origin=None)
    assert status == 403


def test_a_force_on_an_unknown_load_is_refused(served):
    server, paths = served
    status, _ = request(server, "POST", "/force",
                        {"load": "pump", "state": "on", "for": "1h"})
    assert status == 400
    assert files.read_switches(paths.switches)["loads"] == {}


def test_a_duration_the_page_does_not_offer_is_refused(served):
    server, paths = served
    status, _ = request(server, "POST", "/force",
                        {"load": "cameras", "state": "on", "for": "30d"})
    assert status == 400


@pytest.mark.parametrize("address,allowed", [
    ("127.0.0.1", True), ("192.168.50.20", True), ("100.64.0.1", True),
    ("fd7a:115c:a1e0::1", True), ("::ffff:192.168.1.5", True),
    ("8.8.8.8", False), ("2001:4860:4860::8888", False), ("junk", False)])
def test_only_the_tailnet_and_the_site_network_may_use_the_page(
        address, allowed):
    assert page.allowed_client(address) is allowed


def test_the_server_exits_after_a_quiet_spell(tmp_path, monkeypatch):
    monkeypatch.setattr(page, "IDLE_SECONDS", 0.2)
    paths = page.Paths(str(tmp_path / "node.yaml"))
    server = page.Server(("127.0.0.1", 0), page.handler_for(paths))
    thread = threading.Thread(target=server.serve_until_idle, daemon=True)
    thread.start()
    thread.join(timeout=5)
    server.server_close()
    assert not thread.is_alive()
