"""The systemd units: what a test of the code alone can't see."""
from pathlib import Path

import pytest

UNITS = sorted((Path(__file__).parent.parent / "deploy").glob("*.service"))


@pytest.mark.parametrize("unit", UNITS, ids=lambda p: p.name)
def test_no_systemctl_where_run_is_hidden(unit):
    """TemporaryFileSystem=/run also applies to "+" commands: systemctl fails there (once it did so silently)."""
    lines = unit.read_text(encoding="utf-8").splitlines()
    hidden = any(x.startswith("TemporaryFileSystem=") and "/run" in x for x in lines)
    assert not hidden or not [x for x in lines if x.startswith("Exec") and "systemctl" in x]


def test_the_engines_end_with_the_controller():
    controller, *engines = [(UNITS[0].parent / f"ampi-checkout{x}.service").read_text(encoding="utf-8") for x in ("", "-engine", "-lp")]
    stop = next(x for x in controller.splitlines() if x.startswith("ExecStopPost="))
    for text in engines:
        user = next(x for x in text.splitlines() if x.startswith("User=")).partition("=")[2]
        assert f"pkill -KILL -u {user};" in stop and f"pgrep -u {user} " in stop
        assert "\nSuccessExitStatus=SIGKILL\n" in text
