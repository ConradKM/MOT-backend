"""Issue #228: every production call's control connection crashed with
``You need to install `openai[realtime]``` because requirements.txt pinned
plain ``openai`` - the SDK imports ``websockets`` lazily inside
``client.realtime.connect()``, so nothing failed until a real call arrived.
Developer venvs happened to have ``websockets`` installed transitively, which
is why the rest of the suite (which fakes the connection) never noticed."""

import re
from pathlib import Path

REQUIREMENTS = Path(__file__).resolve().parent.parent / "requirements.txt"


def test_requirements_install_the_openai_realtime_extra():
    lines = [
        line.split("#", 1)[0].strip()
        for line in REQUIREMENTS.read_text(encoding="utf-8").splitlines()
    ]
    openai_lines = [line for line in lines if re.match(r"^openai\b", line)]
    assert openai_lines, "openai must be a runtime dependency"
    assert all("[realtime]" in line for line in openai_lines), openai_lines


def test_the_realtime_transport_the_sdk_needs_is_importable():
    # The exact import openai/resources/realtime/realtime.py performs.
    from websockets.sync.client import connect  # noqa: F401
