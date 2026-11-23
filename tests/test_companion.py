"""The companion app's logic that has no Android in it, run with the JDK (skipped without one), and that its pairing code is
the server's."""

import shutil
import subprocess
from pathlib import Path

import pytest

from ampi_phone import trust

ROOT = Path(__file__).resolve().parent.parent / "companion"
JBR = Path("C:/Program Files/Android/Android Studio/jbr/bin")
JAVAC = shutil.which("javac") or (str(JBR / "javac.exe") if (JBR / "javac.exe").exists() else None)
JAVA = shutil.which("java") or (str(JBR / "java.exe") if (JBR / "java.exe").exists() else None)

pytestmark = pytest.mark.skipif(not (JAVAC and JAVA), reason="needs a JDK")


@pytest.fixture(scope="module")
def classes(tmp_path_factory):
    out = tmp_path_factory.mktemp("classes")
    src = ROOT / "src/app/ampi/companion"
    subprocess.run([JAVAC, "-encoding", "UTF-8", "--release", "17", "-d", str(out), *(str(src / f) for f in (
        "Frames.java", "AlarmTimes.java", "Texts.java")), str(ROOT / "test/app/ampi/companion/LogicTest.java")], check=True)
    return out


def test_logic(classes):
    r = subprocess.run([JAVA, "-cp", str(classes), "app.ampi.companion.LogicTest"], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr


def test_pairing_code_is_the_same_on_both_sides(classes):
    r = subprocess.run([JAVA, "-cp", str(classes), "app.ampi.companion.LogicTest", "code"], capture_output=True, text=True)
    assert r.stdout.splitlines() == [trust.pair_code(b"server", b"phone"), trust.pair_code(b"server", b"phone", b"key")]