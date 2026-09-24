"""Keyboard and camera actions.

A click has a coordinate, and a coordinate can be checked: the arbiter refuses
one that falls outside the frame or outside the window. A keystroke and a camera
turn have no coordinate at all, so the bounds check that catches a bad click has
no equivalent here -- a bad key is simply the wrong key, and a bad look delta
spins the player round with nothing to compare it against. What replaces it is
an allowlist and two clamps, and those are what these tests are about.
"""

from __future__ import annotations

import math

import pytest

from gamelens.arbiter import (
    MAX_KEY_HOLD,
    MAX_LOOK_DELTA,
    ActionRejected,
    Arbiter,
    Observation,
    Rejection,
)
from gamelens.input import (
    MOUSEEVENTF_ABSOLUTE,
    Dwell,
    KeyDown,
    KeyUp,
    LookBy,
    key_code,
)


@pytest.fixture
def arbiter() -> Arbiter:
    # None of these actions touch capture, geometry or the executor: they are
    # built from an observation and clamped. Construction is all that is needed.
    return Arbiter(capture=None, geometry_tracker=None, executor=None)


@pytest.fixture
def obs() -> Observation:
    return Observation(
        target_hwnd=1, backend_session_id=1, frame_id=1,
        frame_captured_at=0.0, frame_width=800, frame_height=600,
        geometry_generation=1, preemption_counter=0,
    )


# --- the key allowlist -----------------------------------------------------


def test_named_keys_resolve():
    assert key_code("w") == 0x57
    assert key_code("SPACE") == 0x20
    assert key_code(" Shift ") == 0xA0


@pytest.mark.parametrize("name", ["lwin", "rwin", "win", "menu", "apps", "f12", "pause",
                                  "printscreen", "capslock", "numlock", "scrolllock",
                                  "f13", "ctrl+w", ""])
def test_keys_outside_the_allowlist_are_refused(name):
    """An allowlist, not a lookup with a fallback.

    The Windows and menu keys belong to the shell, F12 and Pause are GameLens's
    own kill switch, and the lock keys outlive the session in the Owner's
    keyboard state. None is input to a game, and none is something an agent
    should be able to reach by naming it.
    """
    with pytest.raises(ValueError):
        key_code(name)


@pytest.mark.parametrize("name,vk", [("f4", 0x73), ("f11", 0x7A), ("delete", 0x2E),
                                     ("tab", 0x09), ("numpad7", 0x67), ("backtick", 0xC0),
                                     ("`", 0xC0), ("/", 0xBF), ("pagedown", 0x22),
                                     ("rctrl", 0xA3), ("backspace", 0x08)])
def test_any_games_keys_are_named(name, vk):
    """GL-040: the table is every key a game binds by default, not Minecraft's
    handful. F4 and Delete were once refused for fear of Alt+F4 and
    Ctrl+Alt+Del; neither chord is reachable -- a single key action holds one
    key, and a sequence cannot press alt (see test_sequence)."""
    assert key_code(name) == vk


# --- clamps ----------------------------------------------------------------


def test_key_hold_is_clamped_not_trusted(arbiter, obs):
    """This is the only action that can hold input down.

    The executor releases held keys on a kill, so an unclamped value is not a
    stuck key forever -- but it is W held for a minute because something
    upstream sent a float it did not mean, and the kill switch should not be
    the thing that catches that.
    """
    action = arbiter.key_action(obs, "w", hold=60.0)
    dwell = next(s for s in action.steps if isinstance(s, Dwell))
    assert dwell.seconds == MAX_KEY_HOLD

    action = arbiter.key_action(obs, "w", hold=-5.0)
    dwell = next(s for s in action.steps if isinstance(s, Dwell))
    assert dwell.seconds > 0, "a negative hold must not become an instant release"


def test_a_key_action_always_releases_what_it_presses(arbiter, obs):
    action = arbiter.key_action(obs, "space")
    kinds = [type(s).__name__ for s in action.steps]
    assert kinds == ["KeyDown", "Dwell", "KeyUp"]
    assert action.steps[0].vk == action.steps[-1].vk


def test_look_delta_is_clamped(arbiter, obs):
    action = arbiter.look_action(obs, 99999, -99999)
    step = action.steps[0]
    assert (step.dx, step.dy) == (MAX_LOOK_DELTA, -MAX_LOOK_DELTA)


def test_a_non_finite_look_is_refused(arbiter, obs):
    """Clamping NaN silently produces a turn of nobody-knows-what."""
    with pytest.raises(ActionRejected) as exc:
        arbiter.look_action(obs, math.nan, 0)
    assert exc.value.reason is Rejection.OUT_OF_BOUNDS


# --- the mechanism ---------------------------------------------------------


def test_a_look_is_a_relative_move(arbiter, obs, monkeypatch):
    """Absolute positioning means nothing to a game holding the cursor.

    It hides the pointer, re-centres it every frame and reads the delta, so an
    absolute move produces whatever difference happens to fall out. The ABSOLUTE
    flag must not be set, or the turn is not the turn that was asked for.
    """
    from gamelens import input as gl_input

    sent: list = []
    monkeypatch.setattr(gl_input, "_send", lambda events: sent.extend(events))

    class _Exec(gl_input.InputExecutor):
        def __init__(self) -> None:          # no safety, no thread
            pass

    _Exec()._apply_new(LookBy(40, -15))
    assert len(sent) == 1
    assert not (sent[0].mi.dwFlags & MOUSEEVENTF_ABSOLUTE)
    assert (sent[0].mi.dx, sent[0].mi.dy) == (40, -15)


def test_an_observation_is_still_required(arbiter, obs):
    """Freshness applies even without a coordinate.

    "Walk forward" is a decision about a scene. A scene two seconds old is one
    the player has already left, and nothing about a keystroke makes that less
    true than it is for a click.
    """
    action = arbiter.key_action(obs, "w")
    assert action.observation is obs
