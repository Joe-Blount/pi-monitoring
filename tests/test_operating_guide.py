"""docs/operating.md is meant to be followed by someone with no one to ask
and no way to check the code. Its whole value is that every command in it
still works, so this holds it against the parsers, the files and the node
files it describes, the same way test_deployment.py holds the README against
the driver registry.

A command is read only out of a fenced code block, never out of prose, so a
sentence that happens to contain the word "control" followed by another word
cannot be mistaken for a command this file has to defend.
"""

import pathlib
import re
import subprocess
import sys

import pytest
import yaml

REPO = pathlib.Path(__file__).resolve().parent.parent
GUIDE = REPO / "docs" / "operating.md"
GUIDE_TEXT = GUIDE.read_text()

COLLECT = REPO / "bin" / "collect"
CONTROL = REPO / "bin" / "control"


def _code_lines(text):
    """Every line inside a fenced code block, stripped.

    Commands live in code blocks; prose does not. Reading only these lines is
    what lets "the control is enabled" and "control run" coexist in the same
    document without the first being mistaken for a second command.
    """
    out = []
    in_block = False
    for line in text.splitlines():
        if line.strip().startswith("```"):
            in_block = not in_block
            continue
        if in_block:
            out.append(line.strip())
    return out


CODE_LINES = _code_lines(GUIDE_TEXT)


# -- collect ------------------------------------------------------------

def _collect_flags_in_guide():
    flags = set()
    for line in CODE_LINES:
        if line.startswith("collect "):
            flags |= set(re.findall(r"--[a-z-]+", line))
    return flags


def test_the_guide_shows_at_least_one_collect_flag():
    """A guard against the next test passing by finding nothing to check."""
    assert _collect_flags_in_guide()


def test_every_collect_flag_the_guide_shows_exists_in_bin_collect():
    help_text = subprocess.run(
        [sys.executable, str(COLLECT), "--help"],
        capture_output=True, text=True, timeout=30).stdout
    shown = _collect_flags_in_guide()
    missing = {flag for flag in shown if flag not in help_text}
    assert not missing, (
        "the guide shows %s, which `collect --help` does not mention; one "
        "of these flags no longer exists" % ", ".join(sorted(missing)))


# -- control --------------------------------------------------------------

def _control_subcommands_in_guide():
    """Every word the guide shows right after "control" inside a code block,
    including inside a quoted pattern such as `pkill -f "control run"`."""
    subcommands = set()
    for line in CODE_LINES:
        text = line
        if text.startswith('"'):
            text = text[1:]
        match = re.match(r'control ([a-z][a-z-]*)', text)
        if match:
            subcommands.add(match.group(1))
        for quoted in re.findall(r'"control ([a-z][a-z-]*)', line):
            subcommands.add(quoted)
    return subcommands


def test_the_guide_shows_at_least_one_control_subcommand():
    assert _control_subcommands_in_guide()


@pytest.mark.skipif(not CONTROL.exists(),
                    reason="bin/control does not exist yet; it is being "
                          "written in parallel")
@pytest.mark.parametrize("subcommand", sorted(_control_subcommands_in_guide()))
def test_every_control_subcommand_the_guide_shows_exists_in_bin_control(
        subcommand):
    result = subprocess.run(
        [sys.executable, str(CONTROL), subcommand, "--help"],
        capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, (
        "`control %s --help` exited %d, so %r is not a subcommand bin/control "
        "recognises:\n%s" % (subcommand, result.returncode, subcommand,
                             result.stderr))


# -- file paths -----------------------------------------------------------

def test_the_node_file_path_the_guide_shows_is_where_install_sh_puts_it():
    install = (REPO / "deploy" / "install.sh").read_text()
    assert "/etc/monitoring/node.yaml" in GUIDE_TEXT
    assert "/etc/monitoring/node.yaml" in install


def test_the_credentials_path_the_guide_shows_matches_the_installed_one():
    install = (REPO / "deploy" / "install.sh").read_text()
    assert "/etc/monitoring/influx.env" in GUIDE_TEXT
    assert "/etc/monitoring/influx.env" in install


def test_the_install_script_path_the_guide_shows_is_where_it_is_installed():
    """install.sh links the repository to /opt/monitoring, so that is the
    path a command typed on an installed machine must use."""
    install = (REPO / "deploy" / "install.sh").read_text()
    assert "/opt/monitoring/deploy/install.sh" in GUIDE_TEXT
    assert "/opt/monitoring" in install
    assert (REPO / "deploy" / "install.sh").exists()


def test_the_published_decision_path_the_guide_shows_matches_files_py():
    from monitoring.control import files
    assert files.PUBLISHED in GUIDE_TEXT, (
        "the guide must read the controller's decisions from the same path "
        "monitoring/control/files.py actually writes them to: %s"
        % files.PUBLISHED)


def test_the_readings_cache_path_the_guide_shows_matches_files_py():
    from monitoring.control import files
    assert files.READINGS_DIR in GUIDE_TEXT, (
        "the guide must name the same reading-cache directory "
        "monitoring/control/files.py actually uses: %s" % files.READINGS_DIR)


# -- GPIO pins --------------------------------------------------------------

def _gpio_table_rows():
    start = GUIDE_TEXT.index("## Which GPIO pin drives which load")
    end = GUIDE_TEXT.index("## Published fields")
    section = GUIDE_TEXT[start:end]
    rows = []
    for line in section.splitlines():
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if cells[0] == "Machine" or set(cells[0]) <= {"-"}:
            continue
        rows.append(cells)
    return rows


def _node_yaml(relative_path):
    return yaml.safe_load((REPO / relative_path).read_text())


#: Where each (machine, load) label in the guide's table actually lives in a
#: node file. Keyed on the guide's own wording, so a renamed row fails here
#: with a clear "no such row" rather than silently checking nothing.
def _pin_sources():
    downstairs = _node_yaml("sites/blind1/downstairs.yaml")
    upstairs = _node_yaml("sites/blind1/upstairs.yaml")
    garage = _node_yaml("sites/garage/garage.yaml")
    return {
        ("blind1 / downstairs", "cameras"):
            downstairs["control"]["loads"]["cameras"]["control_pin"],
        ("blind1 / downstairs", "irrigation: inverter"):
            downstairs["control"]["loads"]["irrigation"]["inverter"]["control_pin"],
        ("blind1 / downstairs", "irrigation: pump"):
            downstairs["control"]["loads"]["irrigation"]["pump"]["control_pin"],
        ("blind1 / upstairs", "vent fan"):
            upstairs["control"]["loads"]["vent_fan"]["control_pin"],
        ("garage", "battery heating"):
            garage["control"]["loads"]["battery_heating"]["control_pin"],
    }


def test_the_guide_shows_at_least_one_gpio_pin():
    assert _gpio_table_rows()


def test_every_gpio_pin_the_guide_shows_matches_the_node_files():
    sources = _pin_sources()
    rows = _gpio_table_rows()
    seen = set()
    for machine, load, gpio, _physical, _wiring in rows:
        key = (machine, load)
        assert key in sources, (
            "the guide's GPIO table names %r / %r, which this test does not "
            "know where to check in a node file; update _pin_sources() if "
            "the row is real" % key)
        seen.add(key)
        assert int(gpio) == sources[key], (
            "the guide says %s on %s drives GPIO%s, but the node file says "
            "GPIO%s" % (load, machine, gpio, sources[key]))
    assert seen == set(sources), (
        "the guide's GPIO table is missing a row for %s"
        % sorted(set(sources) - seen))


# -- house rules ------------------------------------------------------------

def test_the_guide_uses_no_em_dash():
    assert "—" not in GUIDE_TEXT


def test_the_guide_never_mentions_an_assistant():
    lowered = GUIDE_TEXT.lower()
    for word in ("claude", "anthropic", "chatgpt", "copilot", "assistant"):
        assert word not in lowered


def test_the_guide_links_to_bringup_for_the_dht_explanation():
    """The brief asks for a link, not a repeat of the explanation that
    already lives in docs/bringup.md."""
    assert "bringup.md" in GUIDE_TEXT
    assert (REPO / "docs" / "bringup.md").exists()


def test_the_readme_links_to_the_guide():
    readme = (REPO / "README.md").read_text()
    assert "docs/operating.md" in readme


def test_the_guide_names_the_real_service_unit():
    """The guide was written before the unit existed and hedged by telling
    the reader to go and find its name. Naming it is only an improvement
    while the name is right, and a unit renamed in deploy with the guide left
    behind sends somebody hunting for a service that is not there.
    """
    unit = REPO / "deploy" / "monitoring-control.service"
    assert unit.is_file(), "the unit this guide names does not exist"
    name = unit.stem
    # The unit argument itself, compared whole. A containment test passes
    # against a plausible near-miss: the real name is a substring of several
    # names somebody might write instead, so "is it in there" answers yes to
    # a unit that does not exist.
    named = set()
    for command in CODE_LINES:
        words = command.split()
        if "systemctl" not in words:
            continue
        for word in words:
            if "monitoring" in word:
                named.add(word.split(".service")[0])

    assert named, "the guide gives no systemctl command for this service"
    unknown = named - {name, "telegraf"}
    assert not unknown, (
        "the guide names unit(s) %s; the only one that exists is %s"
        % (sorted(unknown), name))
