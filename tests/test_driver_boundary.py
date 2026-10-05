"""A driver may depend on the driver base and nothing else in the project.

The boundary is the reason this is one repository rather than several. A driver
reads one device and returns values; it never learns what InfluxDB is, how a
node file is shaped, or when it is called. While that holds, any driver can be
lifted out into its own package by moving a file. Once a driver reaches into
the runner or the configuration loader, the only way out is a rewrite.

So the boundary is asserted here rather than left as an intention.
"""

import ast
import pathlib

import pytest

from monitoring import drivers

DRIVERS = pathlib.Path(drivers.__file__).parent

#: Modules under drivers/ that a driver may import from this project. Anything
#: else means the driver has grown a dependency on the layer above it.
ALLOWED = {"monitoring.drivers.base", "monitoring.duration"}

DRIVER_FILES = sorted(p for p in DRIVERS.glob("*.py") if p.name != "__init__.py")


def project_imports(path):
    """Every module inside this project that `path` imports.

    Relative imports are resolved against the drivers package, because that is
    how they will read after a move.
    """
    tree = ast.parse(path.read_text(), filename=str(path))
    package = "monitoring.drivers"
    found = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] == "monitoring":
                    found.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0:
                module = node.module or ""
                if module.split(".")[0] == "monitoring":
                    found.add(module)
                continue
            # A relative import: one dot is this package, two is its parent.
            parts = package.split(".")
            base = ".".join(parts[:len(parts) - node.level + 1])
            found.add("%s.%s" % (base, node.module) if node.module else base)

    return found


@pytest.mark.parametrize("path", DRIVER_FILES, ids=lambda p: p.name)
def test_a_driver_depends_only_on_the_driver_base(path):
    reached = project_imports(path)
    assert reached <= ALLOWED, (
        "%s imports %s. A driver may only import %s, so that it stays liftable "
        "into its own package and testable with no hardware."
        % (path.name, ", ".join(sorted(reached - ALLOWED)), ", ".join(sorted(ALLOWED))))


@pytest.mark.parametrize("path", DRIVER_FILES, ids=lambda p: p.name)
def test_a_driver_does_not_build_line_protocol(path):
    """Rendering belongs to one module, which enforces the field types. A
    driver that formats its own output escapes those rules."""
    text = path.read_text()
    assert "lineproto" not in text
    assert "line_protocol" not in text


def test_the_driver_base_depends_on_nothing_in_the_project():
    """The base is what a lifted driver would carry with it, so it must be
    small enough to carry."""
    assert project_imports(DRIVERS / "base.py") == set()


def test_every_driver_is_registered():
    """A module that is never registered cannot be named in a node file, and
    would be dead code that still passes its own tests."""
    modules = {module for module, _ in drivers.REGISTRY.values()}
    for path in DRIVER_FILES:
        if path.name in ("base.py", "stubs.py"):
            continue
        assert "monitoring.drivers.%s" % path.stem in modules, (
            "%s is not in the driver registry" % path.name)
