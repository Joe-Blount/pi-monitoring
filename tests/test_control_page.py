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


def request(server, method, path, form=None, origin=True, host_header=None):
    host, port = server.server_address[:2]
    connection = http.client.HTTPConnection(host, port, timeout=5)
    headers = {}
    if host_header:
        headers["Host"] = host_header
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


def publish(paths, powered=1.0, reason="all tiers clear", **fields):
    entry = {"powered": powered, "reason": reason}
    entry.update(fields)
    files.write_json(paths.published, {
        "fields": {"passes": 1.0}, "boot_id": BOOT,
        "monotonic": files.monotonic(),
        "loads": {"cameras": entry}})


def switches(paths):
    with open(paths.switches) as handle:
        return json.load(handle)


def test_the_page_shows_each_load_with_its_published_state(served):
    server, paths = served
    publish(paths, reason="soc=50.0 above every threshold", shed_level=1.0,
            reason_code=1.0)
    status, text = request(server, "GET", "/")
    assert status == 200
    assert "cameras" in text
    assert "Day only (on 05:00 to 22:00, off at night)" in text
    assert "Below 50%: day only. Below 10%: off." in text
    assert "above every threshold" not in text
    assert "has not published in the last" not in text


def test_a_forced_load_shows_the_mode_it_goes_back_to(served):
    server, paths = served
    publish(paths, shed_level=0.0, reason_code=4.0)
    status, _ = request(server, "POST", "/force",
                        {"load": "cameras", "state": "off", "for": "1h"})
    assert status in (200, 303)
    _, text = request(server, "GET", "/")
    assert "Forced <b>off</b> until" in text
    assert "Then: always on. Battery" in text


def test_a_load_turned_off_in_the_node_file_is_listed_without_buttons(
        tmp_path, monkeypatch):
    node = controlled_node(tmp_path)
    node.write_text(node.read_text() + "    spare:\n      type: dc_branch\n"
                    "      enabled: false\n")
    view = page.View(page.Paths(str(node),
                                switches=str(tmp_path / "switches.json"),
                                published=str(tmp_path / "control.json"),
                                readings=str(tmp_path / "readings"),
                                boot=BOOT))
    text = page.render(view)
    assert "<b>spare</b>" in text
    assert "Turned off in settings" in text
    assert "value='spare'" not in text


def test_the_page_warns_when_the_controller_has_published_nothing(served):
    server, _ = served
    status, text = request(server, "GET", "/")
    assert status == 200
    assert "has not published in the last" in text


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
    ("8.8.8.8", False), ("2001:4860:4860::8888", False),
    ("2001:0:4136:e378::1", False), ("junk", False)])
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


def test_a_request_naming_a_foreign_host_is_refused(served):
    """A name that resolves to this machine but belongs to another site."""
    server, paths = served
    origin = "http://evil.example:%d" % server.server_address[1]
    status, _ = request(server, "POST", "/shedding", {"state": "off"},
                        origin=origin, host_header=origin[7:])
    assert status == 403
    assert files.read_switches(paths.switches)["shedding_enabled"] is True


@pytest.mark.parametrize("host,allowed", [
    ("100.64.0.1:8088", True), ("[fd7a:115c:a1e0::1]:8088", True),
    ("blind1pi.tail1234.ts.net:8088", True), ("evil.example:8088", False),
    ("8.8.8.8:8088", False), ("", False)])
def test_only_this_machines_own_names_are_answered(host, allowed):
    assert page.allowed_host(host) is allowed


def test_a_force_past_its_expiry_can_still_be_cleared(served):
    server, paths = served
    files.write_switches({"loads": {"cameras": {
        "force": "off", "expires_epoch": 1.0}}}, paths.switches)
    _, text = request(server, "GET", "/")
    assert "Forced <b>off</b> expired" in text
    assert "action=/clear" in text


def test_a_silent_connection_does_not_hold_the_page(served, monkeypatch):
    import socket
    server, _ = served
    monkeypatch.setattr(page.Handler, "timeout", 0.3)
    idle = socket.create_connection(server.server_address[:2])
    try:
        status, _ = request(server, "GET", "/")
        assert status == 200
    finally:
        idle.close()


# -- an irrigation load -----------------------------------------------------

IRRIGATION_NODE = """\
site: test
node: test
measurement: test
devices:
  - {name: bms, driver: jbd, mode: poll, params: {address: "AA:BB:CC:DD:EE:FF"}}
  - {name: shunt, driver: qinglan, mode: poll, params: {address: "AA:BB:CC:DD:EE:00"}}
  - {name: mppt, driver: vedirect, mode: resident, params: {port: /dev/null}}
control:
  enabled: true
  relay_trigger: low
  loads:
    irrigation:
%s
"""


@pytest.fixture
def irrigated(tmp_path, monkeypatch):
    import yaml
    from tests.test_irrigation import BLOCK
    monkeypatch.setenv("MONITORING_LOCATION", str(location_file(tmp_path)))
    block = yaml.safe_dump(BLOCK, default_flow_style=False)
    node = tmp_path / "node.yaml"
    node.write_text(IRRIGATION_NODE % "\n".join(
        "      " + line for line in block.splitlines()))
    paths = page.Paths(str(node), switches=str(tmp_path / "switches.json"),
                       published=str(tmp_path / "control.json"),
                       readings=str(tmp_path / "readings"), boot=BOOT)
    server = page.Server(("127.0.0.1", 0), page.handler_for(paths))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server, paths
    server.shutdown()
    server.server_close()


def test_an_irrigation_load_shows_a_run_row_and_an_inverter_row(irrigated):
    server, _ = irrigated
    status, text = request(server, "GET", "/")
    assert status == 200
    assert "<b>irrigation</b>" in text and "<b>irrigation.inverter</b>" in text
    assert ">Run</button>" in text and ">Keep off</button>" in text


def test_a_run_takes_only_the_run_lengths(irrigated):
    server, paths = irrigated
    status, _ = request(server, "POST", "/force",
                        {"load": "irrigation", "state": "on", "for": "4h"})
    assert status == 400
    status, _ = request(server, "POST", "/force",
                        {"load": "irrigation", "state": "on", "for": "30m"})
    assert status == 303
    status, _ = request(server, "POST", "/force",
                        {"load": "irrigation.inverter", "state": "on",
                         "for": "4h"})
    assert status == 303
    assert set(switches(paths)["loads"]) == {"irrigation",
                                             "irrigation.inverter"}


def test_the_lockout_can_be_cleared_from_the_page(irrigated):
    server, paths = irrigated
    status, _ = request(server, "POST", "/clear-lockout",
                        {"load": "irrigation"})
    assert status == 303
    assert "at" in switches(paths)["loads"]["irrigation.clear_lockout"]
    status, _ = request(server, "POST", "/clear-lockout",
                        {"load": "irrigation.inverter"})
    assert status == 400


def test_a_client_the_page_does_not_answer_is_closed_before_it_is_read():
    server = page.Server(("127.0.0.1", 0), page.Handler)
    try:
        assert not server.verify_request(None, ("8.8.8.8", 5000))
        assert server.verify_request(None, ("100.70.1.2", 5000))
    finally:
        server.server_close()
