"""GL-039: several primitives, overlapping in time, as one decision.

Walking while turning cannot be written as one `key` and one `look`: each is a
press-and-release of its own, so the turn begins after the walk has ended. A
sequence is the fix, and it opens three holes that a single action did not have,
which is what most of these tests are about:

* keys overlap, so a sequence could build a chord Windows acts on itself;
* it runs for seconds, so the per-press freshness rule would refuse its own
  second half -- and relaxing that must not relax anything else;
* it holds things down across steps, so it must never finish with any of them
  still held.
"""

from __future__ import annotations

import threading
import time

import pytest

from gamelens import input as gl_input
from gamelens.arbiter import (
    MAX_LOOK_DELTA,
    MAX_SEQUENCE_STEPS,
    SEQUENCE_KEYS,
    Arbiter,
    Rejection,
    parse_sequence,
)
from gamelens.input import (
    INPUT_MOUSE,
    KEYEVENTF_KEYUP,
    MOUSEEVENTF_MOVE,
    Button,
    ButtonDown,
    ButtonUp,
    Dwell,
    KeyDown,
    KeyUp,
    LookBy,
    key_code,
)
from tests.test_arbiter import FakeCapture, FakeExecutor, FakeFrame, FakeTracker
from tests.test_killswitch import Recorder, hwnd, live  # noqa: F401  (fixtures)

W, A, SHIFT, CTRL = key_code("w"), key_code("a"), key_code("shift"), key_code("ctrl")


def parse(spec, capacity=100):
    return parse_sequence(spec, capacity=capacity)


# --- what a sequence turns into ----------------------------------------------


def test_walk_while_turning_is_one_overlapping_sequence():
    steps = parse([
        {"do": "key_down", "key": "w"},
        {"do": "look", "dx": 200, "dy": 0},
        {"do": "wait", "ms": 250},
        {"do": "look", "dx": 200, "dy": 0},
        {"do": "key_up", "key": "w"},
    ])
    assert steps == [KeyDown(W), LookBy(200, 0), Dwell(0.25), LookBy(200, 0), KeyUp(W)]


def test_tap_is_down_wait_up():
    assert parse([{"do": "tap", "key": "space", "ms": 50}]) == [
        KeyDown(0x20), Dwell(0.05), KeyUp(0x20)]


def test_everything_pressed_is_released_at_the_end_in_press_order():
    steps = parse([
        {"do": "key_down", "key": "w"},
        {"do": "key_down", "key": "shift"},
        {"do": "button_down", "button": "left"},
        {"do": "wait", "ms": 100},
    ])
    assert steps[-3:] == [KeyUp(W), KeyUp(SHIFT), ButtonUp(Button.LEFT)]
    downs = {s.vk for s in steps if isinstance(s, KeyDown)}
    ups = {s.vk for s in steps if isinstance(s, KeyUp)}
    assert downs == ups


def test_release_order_is_press_order_across_keys_and_buttons():
    """Button first, then shift: the button comes up first. Grouping releases by
    device would lift shift before the button, changing the modifier state at
    the moment the button is released (GL039-I08)."""
    steps = parse([
        {"do": "button_down", "button": "left"},
        {"do": "key_down", "key": "shift"},
        {"do": "key_down", "key": "w"},
        {"do": "key_up", "key": "shift"},
        {"do": "button_down", "button": "right"},
        {"do": "wait", "ms": 50},
    ])
    assert steps[-3:] == [ButtonUp(Button.LEFT), KeyUp(W), ButtonUp(Button.RIGHT)]


def test_a_key_released_mid_sequence_is_not_released_twice():
    steps = parse([{"do": "key_down", "key": "w"}, {"do": "key_up", "key": "w"},
                   {"do": "look", "dx": 5, "dy": 0}])
    assert steps.count(KeyUp(W)) == 1


def test_look_is_clamped_per_step():
    (step,) = parse([{"do": "look", "dx": 10**6, "dy": -10**6}])
    assert step == LookBy(MAX_LOOK_DELTA, -MAX_LOOK_DELTA)


# --- GL039-R1: no chord Windows would act on ---------------------------------


@pytest.mark.parametrize("chord", [
    ["alt", "tab"],             # Alt+Tab
    ["alt", "f4"],              # Alt+F4
    ["ctrl", "escape"],         # Start menu
    ["ctrl", "shift", "esc"],   # Task Manager
])
def test_windows_shortcut_chords_cannot_be_built(chord):
    spec = [{"do": "key_down", "key": k} for k in chord[:-1]]
    spec.append({"do": "tap", "key": chord[-1]})
    with pytest.raises(ValueError, match="not allowed in a sequence"):
        parse(spec)


@pytest.mark.parametrize("key", ["alt", "ralt", "escape", "esc", "lwin", "f12", "pause"])
def test_keys_outside_the_sequence_allowlist_are_refused(key):
    for do in ("key_down", "tap"):
        with pytest.raises(ValueError, match="not allowed in a sequence"):
            parse([{"do": do, "key": key}])


def test_the_sequence_allowlist_has_no_modifier_but_shift_and_ctrl():
    assert {"alt", "ralt", "escape", "esc"}.isdisjoint(SEQUENCE_KEYS)
    assert {"shift", "ctrl", "rshift", "rctrl", "space", "w"} <= SEQUENCE_KEYS


@pytest.mark.parametrize("key", ["tab", "enter", "f1", "f5", "f11", "delete", "numpad4",
                                 "backspace", "`", "pageup"])
def test_other_games_keys_can_be_held_in_a_sequence(key):
    """GL-040: every KEY_NAMES key but alt and escape. Tab is safe here because
    alt cannot be down: a sequence cannot press it and actions never overlap."""
    vk = gl_input.key_code(key)
    assert parse([{"do": "key_down", "key": "shift"}, {"do": "tap", "key": key}])[1] == KeyDown(vk)


def test_the_sequence_allowlist_is_key_names_minus_the_chord_keys():
    from gamelens.arbiter import SEQUENCE_EXCLUDED_KEYS
    assert SEQUENCE_KEYS == set(gl_input.KEY_NAMES) - SEQUENCE_EXCLUDED_KEYS


# --- malformed input is refused before anything is queued ---------------------


@pytest.mark.parametrize("spec,match", [
    (None, "non-empty list"),
    ([], "non-empty list"),
    ("key_down w", "non-empty list"),
    (["w"], "must be an object"),
    ([{"do": "jump"}], "unknown do"),
    ([{"do": "key_up", "key": "w"}], "was not pressed"),
    ([{"do": "key_down", "key": "w"}, {"do": "key_down", "key": "w"}], "already down"),
    ([{"do": "key_down", "key": "w"}, {"do": "tap", "key": "w"}], "already down"),
    ([{"do": "button_up"}], "was not pressed"),
    ([{"do": "button_down"}, {"do": "button_down"}], "already down"),
    ([{"do": "button_down", "button": "x3"}], "unknown button"),
    ([{"do": "move", "x": 5}], "must be a number"),
    ([{"do": "move", "x": float("nan"), "y": 1}], "finite"),
    ([{"do": "click", "x": 5, "y": "1"}], "must be a number"),
    ([{"do": "button_down"}, {"do": "click"}], "already down"),
    ([{"do": "scroll"}], "clicks must be a number"),
    ([{"do": "scroll", "clicks": 0}], "must not be 0"),
    ([{"do": "scroll", "clicks": 1.5}], "whole number"),
    ([{"do": "scroll", "clicks": True}], "must be a number"),
    ([{"do": "scroll", "clicks": 1, "horizontal": "yes"}], "true or false"),
    ([{"do": "tap", "key": "w"}, {"do": "wait", "ms": -1}], ">= 0"),
    ([{"do": "tap", "key": "w", "ms": -5}], ">= 0"),
    ([{"do": "tap", "key": "w"}, {"do": "wait", "ms": float("inf")}], "finite"),
    ([{"do": "tap", "key": "w"}, {"do": "wait", "ms": float("nan")}], "finite"),
    ([{"do": "tap", "key": "w"}, {"do": "wait", "ms": "100"}], "must be a number"),
    ([{"do": "tap", "key": "w"}, {"do": "wait", "ms": True}], "must be a number"),
    ([{"do": "tap", "key": "w"}, {"do": "wait"}], "must be a number"),
    ([{"do": "look", "dx": None, "dy": 0}], "must be a number"),
    ([{"do": "wait", "ms": 100}], "presses nothing"),
    ([{"do": "tap", "key": "w"}, {"do": "wait", "ms": 5000}], "limit is 5.0s"),
])
def test_malformed_sequences_are_refused(spec, match):
    with pytest.raises(ValueError, match=match):
        parse(spec)


def test_an_integer_too_large_for_a_float_is_a_caller_error():
    with pytest.raises((ValueError, OverflowError)):
        parse([{"do": "tap", "key": "w"}, {"do": "wait", "ms": 10**400}])


def test_too_many_steps_is_refused():
    with pytest.raises(ValueError, match=f"at most {MAX_SEQUENCE_STEPS}"):
        parse([{"do": "look", "dx": 1, "dy": 0}] * (MAX_SEQUENCE_STEPS + 1))


def test_more_new_inputs_than_the_rate_limiter_can_admit_is_refused_whole():
    spec = [{"do": "look", "dx": 1, "dy": 0}] * 11
    with pytest.raises(ValueError, match="--rate"):
        parse(spec, capacity=10)
    assert len(parse(spec[:10], capacity=10)) == 10


def test_auto_releases_do_not_count_against_the_rate_limiter():
    # 10 new inputs and their 10 releases fit a bucket of 10: releases are
    # never rate-limited (release_all proceeds even when the guard denies).
    spec = [{"do": "key_down", "key": k} for k in "wasdqezxcv"]
    assert len(parse(spec, capacity=10)) == 20


# --- freshness is judged at the start, everything else at every press ---------


@pytest.fixture
def quick():
    """An arbiter whose freshness limits are short enough to outlive in a test."""
    capture, tracker, executor = FakeCapture(), FakeTracker(), FakeExecutor()
    arbiter = Arbiter(capture, tracker, executor,
                      observation_deadline=0.05, action_ttl=0.05)
    capture.frames.frame = FakeFrame(1, 1, time.monotonic())
    return capture, tracker, executor, arbiter


def _submitted(quick, *, sequence: bool):
    capture, tracker, executor, arbiter = quick
    capture.frames.frame = FakeFrame(1, 1, time.monotonic())
    frame, obs = arbiter.observe()
    steps = parse([{"do": "key_down", "key": "w"}, {"do": "look", "dx": 5, "dy": 0}])
    action = (arbiter.sequence_action(obs, steps) if sequence
              else arbiter.key_action(obs, "w"))
    assert arbiter.submit(action) is Rejection.OK
    return executor.submitted[-1].validate


def test_a_sequence_is_not_expired_by_its_own_length(quick):
    validate = _submitted(quick, sequence=True)
    validate()                      # first press: fresh
    time.sleep(0.12)                # past both limits
    validate()                      # a later press of the same decision


def test_a_plain_action_still_expires_between_presses(quick):
    from gamelens.arbiter import ActionRejected

    validate = _submitted(quick, sequence=False)
    validate()
    time.sleep(0.12)
    with pytest.raises(ActionRejected) as exc:
        validate()
    assert exc.value.reason in (Rejection.EXPIRED, Rejection.STALE_OBSERVATION)


def test_a_sequence_is_judged_for_age_at_its_first_press(quick):
    from gamelens.arbiter import ActionRejected

    validate = _submitted(quick, sequence=True)
    time.sleep(0.12)                # stalled in the queue before it began
    with pytest.raises(ActionRejected) as exc:
        validate()
    assert exc.value.reason in (Rejection.EXPIRED, Rejection.STALE_OBSERVATION)


def test_a_preemption_mid_sequence_still_stops_it(quick):
    from gamelens.arbiter import ActionRejected

    arbiter = quick[3]
    validate = _submitted(quick, sequence=True)
    validate()
    time.sleep(0.12)
    arbiter.preempt("reflex acted")
    with pytest.raises(ActionRejected) as exc:
        validate()
    assert exc.value.reason is Rejection.PREEMPTED


def test_a_window_move_mid_sequence_still_stops_it(quick):
    from gamelens.arbiter import ActionRejected

    tracker = quick[1]
    validate = _submitted(quick, sequence=True)
    validate()
    tracker.move()
    with pytest.raises(ActionRejected) as exc:
        validate()
    assert exc.value.reason is Rejection.GEOMETRY_MOVED


def test_a_replaced_capture_backend_mid_sequence_still_stops_it(quick):
    from gamelens.arbiter import ActionRejected
    from tests.test_arbiter import FakeBackend

    capture = quick[0]
    validate = _submitted(quick, sequence=True)
    validate()
    capture.backend = FakeBackend(session_id=2)
    with pytest.raises(ActionRejected) as exc:
        validate()
    assert exc.value.reason is Rejection.RETIRED_SESSION


# --- through the real executor ----------------------------------------------


def test_a_sequence_refused_part_way_releases_what_it_held(live, monkeypatch):
    """W goes down, the world moves during the dwell, the turn is refused --
    and W must come back up, with the turn never sent."""
    sup, ex = live
    rec = Recorder()
    monkeypatch.setattr(gl_input, "_send", rec)

    capture, tracker = FakeCapture(), FakeTracker()
    arbiter = Arbiter(capture, tracker, ex)
    capture.frames.frame = FakeFrame(1, 1, time.monotonic())
    _, obs = arbiter.observe()
    steps = parse([{"do": "key_down", "key": "w"}, {"do": "wait", "ms": 300},
                   {"do": "look", "dx": 50, "dy": 0}])
    done = threading.Event()
    outcome = []
    assert arbiter.submit(arbiter.sequence_action(obs, steps),
                          on_outcome=lambda o: (outcome.append(o), done.set())) is Rejection.OK

    # Preempt only once W is really down: the case is a refusal *mid*-sequence.
    deadline = time.monotonic() + 2.0
    while key_code("w") not in ex.snapshot()["pressed_keys"]:
        assert time.monotonic() < deadline, "W was never pressed"
        time.sleep(0.005)
    arbiter.preempt("reflex acted mid-sequence")
    assert done.wait(2.0)

    assert outcome[0].status == "denied"
    # The executor reports the denial and *then* runs release_all (input.py,
    # _run), so the release lands just after the outcome. Bounded wait for it.
    deadline = time.monotonic() + 2.0
    while ex.snapshot()["pressed_keys"] and time.monotonic() < deadline:
        time.sleep(0.005)
    assert rec.count("key", KEYEVENTF_KEYUP) == 1, "W must be released exactly once"
    assert not any(k == "mouse" and f & MOUSEEVENTF_MOVE for k, f, _ in rec.events), (
        "the refused turn must not have been sent")
    assert ex.snapshot()["pressed_keys"] == []


def test_a_completed_sequence_leaves_nothing_held(live, monkeypatch):
    sup, ex = live
    rec = Recorder()
    monkeypatch.setattr(gl_input, "_send", rec)

    capture, tracker = FakeCapture(), FakeTracker()
    arbiter = Arbiter(capture, tracker, ex)
    capture.frames.frame = FakeFrame(1, 1, time.monotonic())
    _, obs = arbiter.observe()
    steps = parse([{"do": "key_down", "key": "w"}, {"do": "button_down"},
                   {"do": "look", "dx": 5, "dy": 0}, {"do": "wait", "ms": 20}])
    done = threading.Event()
    outcome = []
    arbiter.submit(arbiter.sequence_action(obs, steps),
                   on_outcome=lambda o: (outcome.append(o), done.set()))
    assert done.wait(2.0)
    assert outcome[0].status == "sent"
    snap = ex.snapshot()
    assert snap["pressed_keys"] == [] and snap["pressed_buttons"] == []
