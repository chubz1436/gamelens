"""Acceptance A8: what the kill switch does to a sequence already in flight.

This was the last safety claim in the README with no test behind it, and it is
the one that matters most, because both halves can fail in opposite directions:

* A kill that does not reach far enough leaves the press it was trying to stop
  still to come -- worse, arriving *after* cleanup has finished enumerating what
  to release, so nothing will ever release it.
* A kill that reaches too far refuses the key-up as well, and the game is left
  with W held down forever.

``_send`` is stubbed throughout except in the hotkey test: the subject is what
the executor decides to emit, and asserting it for real would mean dragging the
operator's cursor and holding keys on their desktop from inside a test run.
"""

from __future__ import annotations

import ctypes
import threading
import time

import pytest
import win32gui

from gamelens import input as gl_input
from gamelens.input import (
    INPUT_KEYBOARD,
    INPUT_MOUSE,
    KEYEVENTF_KEYUP,
    MOUSEEVENTF_LEFTDOWN,
    MOUSEEVENTF_LEFTUP,
    MOUSEEVENTF_MOVE,
    ButtonDown,
    ButtonUp,
    Dwell,
    InputExecutor,
    KeyDown,
    KeyUp,
    MoveTo,
    Sequence,
    _key_event,
    _send,
)
from gamelens.safety import SafetySupervisor

VK_W = 0x57
# F13. Chosen precisely because nothing binds it: injecting a real F12 to test
# the documented hotkey would open DevTools in whatever browser happened to be
# in front. The polling path under test is identical either way.
VK_F13 = 0x7C


class Recorder:
    """Stands in for _send and classifies what the executor tried to emit."""

    def __init__(self) -> None:
        self.events: list[tuple] = []
        self.lock = threading.Lock()

    def __call__(self, events) -> None:
        with self.lock:
            for ev in events:
                if ev.type == INPUT_MOUSE:
                    self.events.append(("mouse", ev.mi.dwFlags, 0))
                else:
                    self.events.append(("key", ev.ki.dwFlags, ev.ki.wScan))

    def count(self, kind: str, flag: int) -> int:
        with self.lock:
            return sum(1 for k, flags, _ in self.events if k == kind and flags & flag)


@pytest.fixture
def hwnd() -> int:
    return win32gui.GetForegroundWindow()


@pytest.fixture
def live(hwnd: int):
    """An armed, live supervisor and a running executor. Nothing reaches the OS."""
    sup = SafetySupervisor(hwnd, rate=1000.0)
    sup.start()
    ex = InputExecutor(sup, move_settle=0.01, press_hold=0.01)
    ex.start()
    sup.arm()
    sup.go_live()
    try:
        yield sup, ex
    finally:
        ex.shutdown()
        sup.shutdown()


# --- A8: the press that was waiting ---------------------------------------


def test_a_kill_during_a_dwell_is_not_followed_by_the_press(live, monkeypatch):
    """The move happened; the button-down must not.

    The dwell is the whole reason this needs its own test. Checking safety once
    at the top of a sequence would have approved this press a quarter of a
    second before it was due to land, and the kill arrives inside that gap.
    """
    sup, ex = live
    rec = Recorder()
    monkeypatch.setattr(gl_input, "_send", rec)

    settled = threading.Event()
    outcomes: list = []

    ex.submit(Sequence(
        steps=[MoveTo(10, 10), Dwell(0.6), ButtonDown(), Dwell(0.01), ButtonUp()],
        label="test:kill-mid-dwell", meta={"action_id": 1},
        on_outcome=lambda o: (outcomes.append(o), settled.set()),
    ))

    # Well inside the dwell, and long enough that the move has certainly gone.
    time.sleep(0.2)
    assert rec.count("mouse", MOUSEEVENTF_MOVE) >= 1, "the move should already have happened"
    assert rec.count("mouse", MOUSEEVENTF_LEFTDOWN) == 0, "pressed before the dwell elapsed"

    killed_at = time.monotonic()
    sup.kill("A8")

    assert settled.wait(2.0), "the sequence never reported an outcome"
    assert outcomes[0].status == "denied"

    # Promptness is a safety property, not a nicety. A dwell that runs to
    # completion regardless means kill() returns, the cleanup callbacks finish
    # enumerating what to release, and only *then* does the press come up to be
    # refused. Get that ordering wrong once and a press lands after cleanup,
    # with nothing left to release it. The remaining dwell here is 400ms.
    assert time.monotonic() - killed_at < 0.25, (
        "the dwell ran on after the kill instead of being interrupted"
    )

    # The point of the whole exercise.
    time.sleep(0.3)      # past when the dwell would have ended
    assert rec.count("mouse", MOUSEEVENTF_LEFTDOWN) == 0, (
        "a kill was followed by the click it was trying to stop"
    )


def test_cleanup_never_presses_anything_new(live, monkeypatch):
    """Kill with nothing held: the cleanup path must emit nothing at all."""
    sup, ex = live
    rec = Recorder()
    monkeypatch.setattr(gl_input, "_send", rec)

    sup.kill("A8 idle")
    time.sleep(0.2)
    assert rec.events == [], f"cleanup emitted input out of nowhere: {rec.events}"


# --- A8: the key that was already down ------------------------------------


def test_a_key_held_when_the_kill_lands_is_released(live, monkeypatch):
    """The converse failure. Denying the key-up too is how W stays down."""
    sup, ex = live
    rec = Recorder()
    monkeypatch.setattr(gl_input, "_send", rec)

    settled = threading.Event()
    ex.submit(Sequence(
        steps=[KeyDown(VK_W), Dwell(0.6), KeyUp(VK_W)],
        label="test:held-key", meta={"action_id": 2},
        on_outcome=lambda o: settled.set(),
    ))

    time.sleep(0.2)
    assert ex.snapshot()["pressed_keys"] == [VK_W], "the key should be down by now"
    assert rec.count("key", KEYEVENTF_KEYUP) == 0

    sup.kill("A8 held key")
    assert settled.wait(2.0)

    assert rec.count("key", KEYEVENTF_KEYUP) == 1, (
        "the held key must be released exactly once -- never zero (stuck key), "
        "never twice (the cleanup and the sequence's own key-up both firing)"
    )
    snap = ex.snapshot()
    assert snap["pressed_keys"] == []
    assert snap["unreleased"] == [], "tracking must not claim a key is still held"


def test_a_held_button_is_released_too(live, monkeypatch):
    sup, ex = live
    rec = Recorder()
    monkeypatch.setattr(gl_input, "_send", rec)
    monkeypatch.setattr(gl_input, "cursor_position", lambda: (10, 10))

    settled = threading.Event()
    ex.submit(Sequence(
        steps=[MoveTo(10, 10), ButtonDown(), Dwell(0.6), ButtonUp()],
        label="test:held-button", meta={"action_id": 3},
        on_outcome=lambda o: settled.set(),
    ))
    time.sleep(0.2)
    assert ex.snapshot()["pressed_buttons"] == ["LEFT"]

    sup.kill("A8 held button")
    assert settled.wait(2.0)
    assert rec.count("mouse", MOUSEEVENTF_LEFTUP) == 1
    assert ex.snapshot()["pressed_buttons"] == []


def test_a_failed_release_is_not_forgotten(live, monkeypatch):
    """If the OS refuses the key-up, tracking must still say the key is held.

    Clearing the set first would mean a later cleanup sees nothing to do and the
    snapshot reports all clear while the game still has the key down -- the
    stuck-key failure, with the evidence erased.
    """
    sup, ex = live

    def refuse(events):
        if events and events[0].type == INPUT_KEYBOARD and \
                events[0].ki.dwFlags & KEYEVENTF_KEYUP:
            raise gl_input.InjectionFailed("simulated UIPI refusal")

    monkeypatch.setattr(gl_input, "_send", refuse)

    settled = threading.Event()
    ex.submit(Sequence(
        steps=[KeyDown(VK_W), Dwell(0.6), KeyUp(VK_W)],
        label="test:refused-release", meta={"action_id": 4},
        on_outcome=lambda o: settled.set(),
    ))
    time.sleep(0.2)
    sup.kill("A8 refused release")
    assert settled.wait(2.0)

    snap = ex.snapshot()
    assert snap["pressed_keys"] == [VK_W], "a refused release must not clear tracking"
    assert snap["unreleased"], "the failure must be visible on the dashboard"


# --- the hotkey itself -----------------------------------------------------


def test_the_hotkey_latches_the_kill(hwnd):
    """End to end: a real key event reaches the watchdog's GetAsyncKeyState poll.

    Everything else here tests kill() being called. This tests the thing that
    calls it -- the only part of the documented kill switch that a person
    pressing F12 actually exercises.
    """
    sup = SafetySupervisor(hwnd, rate=1000.0, kill_keys=(VK_F13,))
    sup.start()
    try:
        sup.arm()
        assert not sup.killed

        _send([_key_event(VK_F13, up=False)])
        try:
            deadline = time.monotonic() + 2.0
            while not sup.killed and time.monotonic() < deadline:
                time.sleep(0.01)
        finally:
            # Always, even on failure: leaving a key down on the operator's
            # machine is exactly the defect these tests are about.
            _send([_key_event(VK_F13, up=True)])

        assert sup.killed, "the watchdog did not see the kill key"
        assert "vk=" in sup.snapshot()["kill_reason"]
    finally:
        sup.shutdown()


def test_the_latch_survives_the_key_being_released(hwnd):
    """It is a latch, not a hold. Letting go must not re-enable anything."""
    sup = SafetySupervisor(hwnd, rate=1000.0, kill_keys=(VK_F13,))
    sup.start()
    try:
        sup.arm()
        _send([_key_event(VK_F13, up=False)])
        deadline = time.monotonic() + 2.0
        while not sup.killed and time.monotonic() < deadline:
            time.sleep(0.01)
        _send([_key_event(VK_F13, up=True)])
        time.sleep(0.2)
        assert sup.killed
        assert not sup.armed
    finally:
        sup.shutdown()
