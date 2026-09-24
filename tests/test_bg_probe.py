"""GL-039 B1: the background-input probe must not be able to answer wrongly.

The 2026-09-22 answer was wrong because the world was paused, and nothing in
that probe could have noticed. These tests hold this one to the rule that
replaced it: a case is `supported` or `unsupported` only when every check around
it passed and the motion is attributable to the input; otherwise it -- and,
where the state itself is in doubt, everything after it -- is `inconclusive`.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import bg_input_probe as probe  # noqa: E402

H, W = 72, 128
WORLD = np.full((H, W, 3), 80, np.uint8)
MENU = np.full((H, W, 3), 20, np.uint8)


def level(v: int) -> np.ndarray:
    return np.full((H, W, 3), v % 200 + 30, np.uint8)


def centre_changed(base: np.ndarray, v: int) -> np.ndarray:
    img = base.copy()
    img[H * 2 // 5:H * 3 // 5, W * 2 // 5:W * 3 // 5] = v
    return img


def state(session=1, forced="printwindow", backend=None, drop=None, frame_id=0) -> dict:
    capture = {"backend": backend or forced or "wgc", "session_id": session,
               "forced_backend": forced, "frame_id": frame_id}
    if drop:
        capture.pop(drop)
    return {"capture": capture, "target": {"hwnd": 1}}


class FakeIO:
    """A scripted game. Frame ids advance on every read; `react` decides what a
    posted message does to the screen; `drift` moves the scene by itself."""

    def __init__(self) -> None:
        self.screen = WORLD
        self.session = 1
        self.forced = "printwindow"
        self.fid = 100
        self.fg = False
        self.posted: list = []
        self.events: list = []            # ordered log: ("post", msg) / ("marker", id) / ...
        self.react = lambda io, msg, wp: None
        self.drift = None                 # callable(io) run on every sleep
        self.on_sleep = None

    def state(self):
        self.fid += 1
        self.events.append(("marker", self.fid))
        return state(self.session, self.forced, frame_id=self.fid)

    def frame(self, after=None):
        self.fid += 1
        if after is not None:
            assert self.fid > after
            self.events.append(("after", after))
        return self.screen, self.fid

    def foreground(self):
        return self.fg

    def post(self, msg, wparam, lparam):
        self.posted.append((msg, wparam, lparam))
        self.events.append(("post", msg))
        self.react(self, msg, wparam)

    def in_world(self, arr):
        return arr is not MENU

    def client_size(self):
        return 800, 600

    def scan(self, vk):
        return 0x11

    def sleep(self, s):
        if self.drift:
            self.drift(self)
        if self.on_sleep:
            self.on_sleep(self)

    def save(self, name, arr):
        return name


def run(io, trials=2):
    return probe.Probe(io, trials=trials).run()


def verdicts(result) -> dict:
    return {c["name"]: c["verdict"] for c in result["cases"]}


# --- the capture must be the game's, and stay the game's ------------------------


@pytest.mark.parametrize("st,match", [
    (state(forced=None), "not forced"),
    (state(forced="mss"), "only"),
    (state(drop="forced_backend"), "predates"),
    (state(drop="session_id"), "predates"),
    (state(forced="printwindow", backend="mss"), "but running"),
    ({}, "no capture"),
])
def test_it_refuses_capture_that_could_show_another_window(st, match):
    io = FakeIO()
    io.state = lambda: st
    result = run(io)
    assert match in result["refused"]
    assert io.posted == [], "a refused probe must inject nothing"


def test_a_session_change_during_a_hold_makes_it_and_later_cases_inconclusive():
    """The frame still shows a hotbar, so in_world() alone would pass it: only
    the session comparison catches that it came from a different capture."""
    io = FakeIO()

    def react(io, msg, wp):
        if msg == probe.WM_KEYDOWN and wp == probe.VK_W:
            io.screen = level(200)                       # hotbar-visible, "moved"
            io.session = 2                               # ...from a new session

    io.react = react
    # One trial, so the check after the case is the only thing that can catch
    # it -- a second trial's precheck would otherwise hide a missing postcheck.
    result = run(io, trials=1)
    got = verdicts(result)
    assert got["w_hold"] == got["left_hold"] == got["e_inventory"] == "inconclusive"
    (w,) = [c for c in result["cases"] if c["name"] == "w_hold"]
    assert "session" in w["reason"]


def test_a_foreground_game_is_not_a_background_test():
    io = FakeIO()
    io.fg = True
    got = verdicts(run(io))
    assert set(got.values()) == {"inconclusive"}
    assert io.posted == []


def test_a_paused_world_is_detected_not_scored():
    """The 2026-09-22 confound: every case against a menu reads 'no effect'."""
    io = FakeIO()
    io.screen = MENU
    result = run(io)
    assert set(verdicts(result).values()) == {"inconclusive"}
    assert io.posted == []
    # Refused at the start, before a control interval is spent on it.
    assert "paused" in result["cases"][0]["reason"]


# --- GL039-I06: focus taken in the middle of a case ------------------------------


def test_activation_during_a_hold_makes_the_case_inconclusive():
    """The Owner clicks the game mid-hold and away again before it ends: the
    rest of W was focused input, and its effect says nothing about background
    input. Checking the foreground only before and after the case misses this."""
    io = FakeIO()

    def focused_hold(io):
        io.fg = True
        io.screen = level(int(io.screen[0, 0, 0]) + 40)

    def react(io, msg, wp):
        if msg == probe.WM_KEYDOWN and wp == probe.VK_W:
            io.on_sleep = focused_hold
        if msg == probe.WM_KEYUP and wp == probe.VK_W:
            io.on_sleep = None
            io.fg = False                                # clicked away again

    io.react = react
    result = run(io, trials=1)
    got = verdicts(result)
    assert got["w_hold"] == "inconclusive"
    assert got["left_hold"] == got["e_inventory"] == "inconclusive"
    (w,) = [c for c in result["cases"] if c["name"] == "w_hold"]
    assert "foreground" in w["reason"]


# --- GL039-I07: motion must be attributable to the input ---------------------------


def test_a_scene_moving_by_itself_does_not_vouch_for_ignored_input():
    """Every message ignored, the world animating on its own (rain, a mob):
    whole-image churn is high throughout, and must not read as support."""
    io = FakeIO()
    io.drift = lambda io: setattr(io, "screen", level(int(io.screen[0, 0, 0]) + 25))
    got = verdicts(run(io))
    for name in ("mouse_move", "w_hold", "left_hold"):
        assert got[name] == "inconclusive", name


def test_no_lasting_change_is_inconclusive_not_unsupported():
    """GL039-I09: ignored input and accepted input with no lasting effect (W
    into a wall, a hold shorter than the block's break time) look the same, so
    a still screen proves nothing. Only E, whose accepted press always opens
    the inventory, may say unsupported."""
    io = FakeIO()
    result = run(io)
    got = verdicts(result)
    assert got == {"mouse_move": "inconclusive", "w_hold": "inconclusive",
                   "left_hold": "inconclusive", "e_inventory": "unsupported"}
    (w,) = [c for c in result["cases"] if c["name"] == "w_hold"]
    assert "not proof it was ignored" in w["reason"]
    for case in result["cases"]:
        for trial in case["trials"]:
            assert trial["verdict"] != "unsupported", (case["name"], trial)


def test_mining_seen_only_while_the_button_is_held_is_supported():
    """Cracks animate during the hold and vanish on release when the block did
    not break: the mid-hold frame is the evidence (GL039-I09)."""
    io = FakeIO()
    base = {"img": WORLD}
    presses = {"n": 0}

    def react(io, msg, wp):
        if msg == probe.WM_LBUTTONDOWN:
            presses["n"] += 1
            base["img"] = io.screen
            io.screen = centre_changed(io.screen, (presses["n"] * 90) % 256)
        if msg == probe.WM_LBUTTONUP:
            io.screen = base["img"]                      # the cracks go away

    io.react = react
    assert verdicts(run(io))["left_hold"] == "supported"


def test_a_menu_opening_during_the_control_stops_the_input():
    """GL039-I10: preconditions are re-checked on the frame the input will be
    judged from, after the control interval -- not only before it."""
    io = FakeIO()
    io.on_sleep = lambda io: setattr(io, "screen", MENU)
    got = verdicts(run(io, trials=1))
    assert set(got.values()) == {"inconclusive"}
    assert io.posted == []


def test_focus_during_the_control_stops_the_input():
    io = FakeIO()
    io.on_sleep = lambda io: setattr(io, "fg", True)
    got = verdicts(run(io, trials=1))
    assert set(got.values()) == {"inconclusive"}
    assert io.posted == []


def test_focus_mid_sweep_stops_new_input_but_not_releases():
    """Once the game is seen in front, no further new input goes to it --
    anything more would be focused input -- but releases still do."""
    io = FakeIO()
    moves = {"n": 0}

    def react(io, msg, wp):
        if msg == probe.WM_MOUSEMOVE:
            moves["n"] += 1
            if moves["n"] == 5:
                io.fg = True

    io.react = react
    got = verdicts(run(io, trials=1))
    assert got["mouse_move"] == "inconclusive"
    assert moves["n"] == 5, "no mouse move may follow the focus sighting"
    assert not any(m in (probe.WM_KEYDOWN, probe.WM_LBUTTONDOWN) for m, _, _ in io.posted)


def test_a_key_held_when_focus_is_seen_is_still_released():
    io = FakeIO()

    def react(io, msg, wp):
        if msg == probe.WM_KEYDOWN and wp == probe.VK_W:
            io.on_sleep = lambda io: setattr(io, "fg", True)

    io.react = react
    run(io, trials=1)
    ups = [w for m, w, _ in io.posted if m == probe.WM_KEYUP]
    assert ups == [probe.VK_W]


def test_effects_are_supported():
    io = FakeIO()
    blocks = {"n": 0}

    def react(io, msg, wp):
        if msg == probe.WM_KEYDOWN and wp == probe.VK_W:
            io.screen = level(int(io.screen[0, 0, 0]) + 40)         # the player moved
        if msg == probe.WM_LBUTTONDOWN:
            blocks["n"] += 1                                         # a different block each time
            io.screen = centre_changed(io.screen, (blocks["n"] * 90) % 256)
        if msg == probe.WM_KEYDOWN and wp == probe.VK_E:
            io.screen = MENU if io.screen is not MENU else WORLD

    io.react = react
    got = verdicts(run(io))
    assert got == {"mouse_move": "inconclusive", "w_hold": "supported",
                   "left_hold": "supported", "e_inventory": "supported"}


def test_a_mining_hold_that_moves_the_whole_frame_is_not_mining():
    """Left hold 'works' but the change is everywhere, not at the crosshair --
    that is the camera or the scene, not a block breaking."""
    io = FakeIO()

    def react(io, msg, wp):
        if msg == probe.WM_LBUTTONDOWN:
            io.screen = level(int(io.screen[0, 0, 0]) + 60)

    io.react = react
    assert verdicts(run(io))["left_hold"] == "inconclusive"


def test_trials_that_disagree_are_inconclusive():
    io = FakeIO()
    presses = {"n": 0}

    def react(io, msg, wp):
        if msg == probe.WM_KEYDOWN and wp == probe.VK_W:
            presses["n"] += 1
            if presses["n"] == 1:                       # only the first W did anything
                io.screen = level(int(io.screen[0, 0, 0]) + 40)

    io.react = react
    assert verdicts(run(io))["w_hold"] == "inconclusive"


# --- GL039-I04: the after-frame is counted from the end of the input ---------------


def test_after_frames_are_counted_from_a_marker_taken_after_the_release():
    io = FakeIO()
    run(io)
    ev = io.events
    for i, (kind, value) in enumerate(ev):
        if kind != "after":
            continue
        marker_at = max(j for j in range(i) if ev[j] == ("marker", value))
        posts_before = [j for j in range(i) if ev[j][0] == "post"]
        if posts_before:
            assert marker_at > max(posts_before), (
                "the marker must be read after the last message of the case was posted")


# --- GL039-I05: a failed restore is not a result ------------------------------------


def test_an_inventory_that_does_not_close_is_inconclusive():
    io = FakeIO()

    def react(io, msg, wp):
        if msg == probe.WM_KEYDOWN and wp == probe.VK_E:
            io.screen = MENU                             # opens, never closes

    io.react = react
    result = run(io)
    (e,) = [c for c in result["cases"] if c["name"] == "e_inventory"]
    assert e["verdict"] == "inconclusive" and "not restored" in e["reason"]


def test_releases_are_posted_even_when_injection_raises():
    io = FakeIO()

    def boom(io, msg, wp):
        if msg == probe.WM_KEYDOWN and wp == probe.VK_W:
            raise RuntimeError("post failed")

    io.react = boom
    with pytest.raises(RuntimeError):
        run(io)
    assert (probe.WM_KEYUP, probe.VK_W) in [(m, w) for m, w, _ in io.posted]


def test_the_restoring_press_is_released_even_when_it_raises():
    io = FakeIO()
    count = {"e": 0}

    def react(io, msg, wp):
        if msg == probe.WM_KEYDOWN and wp == probe.VK_E:
            count["e"] += 1
            if count["e"] == 1:
                io.screen = MENU
            else:
                raise RuntimeError("post failed")

    io.react = react
    with pytest.raises(RuntimeError):
        run(io)
    ups = [(m, w) for m, w, _ in io.posted if m == probe.WM_KEYUP and w == probe.VK_E]
    assert len(ups) == 2


# --- small pieces ----------------------------------------------------------------------


def test_key_messages_carry_a_real_keystrokes_bits():
    assert probe.key_lparam(0x57, False, lambda vk: 0x11) == 1 | 0x11 << 16
    up = probe.key_lparam(0x57, True, lambda vk: 0x11)
    assert up & (1 << 30) and up & (1 << 31) and (up >> 16) & 0xFF == 0x11


def test_the_probe_cannot_inject_through_sendinput_or_arm():
    source = (Path(probe.__file__)).read_text(encoding="utf-8")
    code = source.split('"""', 2)[2]                    # past the module docstring
    for forbidden in ("SendInput", "gamelens.input", '"/arm"', '"/live"', "keybd_event"):
        assert forbidden not in code


def test_an_animation_at_the_crosshair_does_not_certify_ignored_mining():
    """GL040-RV02: something animating in the centre fifth by itself (a mob, a
    flame) moves the centre a lot and the whole frame little -- exactly what
    at_crosshair looks for -- with every posted input ignored. The control
    measures the same region, so this must not come out supported."""
    io = FakeIO()
    tick = {"n": 0}

    def drift(io):
        tick["n"] += 1
        # Up to 50 grey levels in the centre: the whole frame moves by about
        # 2, under the margin, so only the centre test can be fooled.
        io.screen = centre_changed(WORLD, 80 + (tick["n"] * 17) % 51)

    io.drift = drift
    io.react = lambda io, msg, wp: None               # input ignored
    result = run(io, trials=1)
    (case,) = [c for c in result["cases"] if c["name"] == "left_hold"]
    (trial,) = case["trials"]
    assert trial["control"] <= probe.MARGIN, "the scene test must not be what refuses it"
    assert case["verdict"] != "supported"


def test_an_animation_seen_only_mid_interval_is_matched_by_the_control_mid_sample():
    """GL040-RV02, the part the end-of-interval control cannot see: the centre
    lights up in the middle of every interval and is dark again by its end.
    The mining hold's mid-hold frame catches it; a control sampled only at its
    end would say the centre never moved, and the ignored hold would pass. The
    control therefore samples at the same offset as the input's mid frame."""
    io = FakeIO()
    tick = {"n": None}

    def drift(io):
        if tick["n"] is None:
            return
        tick["n"] += 1
        # left_hold: control 30 slices to its mid sample, 35 more to its end;
        # the input the same. Bright from slice 15 to 44 of each 65.
        io.screen = centre_changed(WORLD, 130) if 15 <= tick["n"] % 65 < 45 else WORLD

    def react(io, msg, wp):
        if msg == probe.WM_KEYUP and wp == probe.VK_W:      # w_hold is over:
            tick["n"] = 0                                     # left_hold's control starts next

    io.drift = drift
    io.react = react
    result = run(io, trials=1)
    (case,) = [c for c in result["cases"] if c["name"] == "left_hold"]
    (trial,) = case["trials"]
    assert trial["control"] <= probe.MARGIN
    assert trial["control_centre"] > probe.MARGIN, "the control saw the mid-interval flash"
    assert case["verdict"] != "supported"
