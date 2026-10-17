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
    including behind a sudo prefix and inside a quoted pattern such as
    `pkill -f "control run"`.

    The sudo prefix matters to this parser rather than only to the reader: an
    anchored match would find nothing once the guide shows the commands the
    way somebody has to type them, and every subcommand would silently stop
    being checked against bin/control.
    """
    subcommands = set()
    for line in CODE_LINES:
        text = line
        if text.startswith('"'):
            text = text[1:]
        if text.startswith("sudo "):
            text = text[len("sudo "):]
        match = re.match(r'control ([a-z][a-z-]*)', text)
        if match:
            subcommands.add(match.group(1))
        for quoted in re.findall(r'"(?:sudo )?control ([a-z][a-z-]*)', line):
            subcommands.add(quoted)
    return subcommands


#: The subcommands the guide is known to show. A floor under the parser, not
#: a second copy of its job.
#:
#: Every check below that walks the parsed set shrinks silently when the
#: parser stops matching: the parametrization just gets smaller and the file
#: still passes, with fewer tests and no failure anywhere. Anything that
#: changes the shape of a command line in the guide can cause it, a prefix on
#: every command most of all, and the parser cannot report what it failed to
#: recognise.
GUIDE_SUBCOMMANDS = {"dry-run", "override", "run", "shedding"}


def test_the_guide_shows_the_control_subcommands_it_is_known_to_show():
    """The floor under the parser. Without it, a parser that matches nothing
    leaves every subcommand unchecked against bin/control and says so by
    running fewer tests, which nothing reads."""
    found = _control_subcommands_in_guide()
    missing = GUIDE_SUBCOMMANDS - found
    assert not missing, (
        "the parser does not find %s in the guide, so those are not being "
        "checked against bin/control" % sorted(missing))


@pytest.mark.skipif(not CONTROL.exists(),
                    reason="bin/control is not present in this tree")
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


def test_every_control_command_the_guide_shows_carries_sudo():
    """The overrides, the shedding switch and the readings the controller
    decides from all live where only the service user may read and write.

    A command shown without it fails with a permission error for the person
    this guide is written for: somebody standing at a Pi, logged in as
    themselves, with nobody to ask. The installer puts no login user in the
    service group, so being told to type the command that cannot work is
    worse than being told nothing.
    """
    shown = [line for line in CODE_LINES
             if line.startswith("control ") or line.startswith("sudo control ")]
    assert shown, "the guide shows no control command, so this proves nothing"
    bare = [line for line in shown if not line.startswith("sudo ")]
    assert bare == [], "shown without sudo: %s" % "; ".join(bare)


def _linked_onto_the_path(install):
    """Every tool the installer links into a command search path.

    Comments are stripped first. The installer explains itself at length, so
    a search of the raw text finds the path it talks about as readily as the
    path it creates, and a test that cannot tell those apart passes against
    an installer that only describes the intention.
    """
    code = [line.split("#", 1)[0] for line in install.splitlines()]
    linked = set()
    loop_names = []
    for line in code:
        match = re.match(r"\s*for\s+(\w+)\s+in\s+([\w\s]+?);\s*do", line)
        if match:
            loop_names = match.group(2).split()
        if "ln " in line and "/usr/local/bin" in line:
            for name in re.findall(r"/usr/local/bin/(\$?\{?\w+\}?)", line):
                if name.startswith("$"):
                    linked |= set(loop_names)
                else:
                    linked.add(name)
    return linked


def test_every_bare_command_the_guide_shows_is_put_on_the_path():
    """The guide tells somebody to type `collect` and `sudo control`, so the
    installer has to make those resolve.

    Nothing links them by default: the installer points /opt/monitoring at
    the checkout, and that directory is on nobody's command search path, nor
    on the one sudo substitutes for root. Every command in this document
    would stop with "command not found" for the person it is written for,
    which is the worst way for a field guide to be wrong, because it reads as
    though the machine is broken rather than the instructions.

    Checked against the installer rather than by running anything, since what
    is asserted is a property of a machine this suite is not running on.
    """
    bare = set()
    for line in CODE_LINES:
        words = line.replace('"', "").split()
        if words and words[0] == "sudo":
            words = words[1:]
        if words and "/" not in words[0]:
            bare.add(words[0])
    bare &= {path.name for path in (REPO / "bin").iterdir()}
    assert bare, "the guide shows no bare command, so this proves nothing"

    linked = _linked_onto_the_path(
        (REPO / "deploy" / "install.sh").read_text())
    missing = bare - linked
    assert not missing, (
        "the guide says to type %s, and the installer puts %s on the command "
        "search path, so those will not resolve"
        % (sorted(missing), sorted(linked) or "nothing"))


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

#: Everything the repository publishes, which is every file it tracks. The
#: two house rules below are not about the guide: they are about anything a
#: reader can see, and the guide was only the first place they were checked.
def tracked_files():
    listed = subprocess.run(["git", "ls-files", "-z"], cwd=str(REPO),
                            capture_output=True, text=True)
    if listed.returncode != 0:                          # pragma: no cover
        pytest.skip("not a git checkout")
    return [REPO / name for name in listed.stdout.split("\0") if name]


def readable_text():
    """Every tracked file this can read as text, with its path."""
    for path in tracked_files():
        try:
            yield path, path.read_text()
        except (OSError, UnicodeDecodeError):           # pragma: no cover
            continue


#: The forbidden literals, assembled rather than written out.
#:
#: Each rule below applies to every tracked file, this one included, so a
#: guard that spelled its own target would have to exempt itself and an
#: exemption is a hole: the one file nobody checks is the file most likely to
#: be edited when a rule is being worked around. Built from pieces, these
#: never appear in the repository, so nothing needs exempting and the guards
#: cover themselves.
EM_DASH = chr(0x2014)
FORBIDDEN_NAMES = ("cl" + "aude", "anthro" + "pic", "chat" + "gpt",
                   "cop" + "ilot", "assist" + "ant")

#: Anything more precise about this place than the tenth of a degree the
#: repository is allowed to hold. Checked as text rather than only on the
#: keys that carry a value, because a position is published just as
#: completely by a worked example in a comment, an error message or a test
#: docstring, and in degrees and minutes it does not look like a coordinate
#: at all.
PRECISE_DEGREES = re.compile(r"-?\b(?:37|97)\.[0-9]{2,}")


def test_no_tracked_file_uses_an_em_dash():
    """Two hyphens instead, everywhere. It is a rule about the writing
    rather than about one document, and the character arrives by paste and
    by autocorrect, so the file it lands in is whichever one was open."""
    found = [str(path.relative_to(REPO)) for path, text in readable_text()
             if EM_DASH in text]
    assert found == [], "em dash in %s" % ", ".join(sorted(found))


def test_no_tracked_file_names_a_model_or_its_maker():
    """The work stands on its own. Checked across the repository rather than
    in the guide alone, since a name turns up in a comment as readily as in
    prose.

    The identifiers here are deliberately neutral. This guard covers its own
    file, so a name spelled out to describe the rule would trip it, and the
    answer to that is a better name rather than an exemption.
    """
    found = []
    for path, text in readable_text():
        lowered = text.lower()
        found += ["%s: %s" % (path.relative_to(REPO), word)
                  for word in FORBIDDEN_NAMES if word in lowered]
    assert found == [], "; ".join(sorted(found))


def test_no_tracked_file_carries_a_position_more_precise_than_agreed():
    """A tenth of a degree is about ten kilometres of fuzz and under a
    minute of error in a sunrise, which is the whole precision this
    repository may hold. It is public."""
    found = []
    for path, text in readable_text():
        for number in set(PRECISE_DEGREES.findall(text)):
            found.append("%s: %s" % (path.relative_to(REPO), number))
    assert found == [], "; ".join(sorted(found))


def test_the_guide_links_to_bringup_for_the_dht_explanation():
    """The brief asks for a link, not a repeat of the explanation that
    already lives in docs/bringup.md."""
    assert "bringup.md" in GUIDE_TEXT
    assert (REPO / "docs" / "bringup.md").exists()


def test_the_readme_links_to_the_guide():
    readme = (REPO / "README.md").read_text()
    assert "docs/operating.md" in readme


def test_the_guide_names_the_real_service_unit():
    """Naming the unit is only a kindness while the name is right. A unit
    renamed under deploy with the guide left behind sends somebody standing
    at a machine hunting for a service that is not there, which is worse
    than telling them to look it up.
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
