"""Settings every test shares."""

import pytest

from monitoring.control import relay


@pytest.fixture(autouse=True)
def kernel_releases_freed_outputs(tmp_path, monkeypatch):
    """Tests run as on a kernel that frees an output as an input, whatever
    the machine running them reports. A test about the other kind of kernel
    points the setting at a file of its own."""
    monkeypatch.setattr(relay, "PERSIST_PARAMETER",
                        str(tmp_path / "no-such-kernel-setting"))
