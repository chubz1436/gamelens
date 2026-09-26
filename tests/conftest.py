"""Shared test setup.

The executor's per-press guards (GL040-I01, I03) read the real cursor and the
real keyboard. A test run must not pass or fail on where the Owner's mouse
happens to be, so by default the pointer is on the target and nothing is held;
tests of the guards themselves set these explicitly.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _quiet_desktop(monkeypatch):
    from gamelens import input as gl_input

    monkeypatch.setattr(gl_input, "pointer_on_window", lambda hwnd: True)
    monkeypatch.setattr(gl_input, "held_keys", lambda vks: set())
