"""GL-040: controls for any game, not only Minecraft.

What the first controls could not say, and most games need: the wheel (weapon
and hotbar cycling, zoom), the side mouse buttons, the rest of the keyboard,
and -- for any game played with a visible cursor -- pointing inside a sequence:
drag, shift-click, hover. Pointing is the part with safety rules, because a
point is a coordinate: every one goes through the click's bounds and geometry
checks, and on a rebind every one must still look as it was shown.
"""

from __future__ import annotations

import threading
import time

import pytest

from gamelens import input as gl_input
from gamelens.app import Dispatch, GameLens
from gamelens.arbiter import (
    CLICK_HOLD,
    CLICK_SETTLE,
    MAX_SCROLL_CLICKS,
    ActionRejected,
    Arbiter,
    PointAt,
    Rejection,
    parse_sequence,
    sequence_points,
)
from gamelens.input import (
    MOUSEEVENTF_HWHEEL,
    MOUSEEVENTF_WHEEL,
    MOUSEEVENTF_XDOWN,
    MOUSEEVENTF_XUP,
    Button,
    ButtonDown,
    ButtonUp,
    Dwell,
    KeyDown,
    MoveTo,
    Scroll,
    button_from_name,
    key_code,
)
from tests.test_arbiter import FakeCapture, FakeExecutor, FakeFrame, FakeTracker
from tests.test_killswitch import hwnd, live  # noqa: F401  (fixtures)
from tests.test_rebind import BUTTON, later, lens, scene, shown  # noqa: F401


def parse(spec, capacity=100):
    return parse_sequence(spec, capacity=capacity)


class MappedGeometry:
    """A window whose frame starts at screen (100, 50) and is 1920x1080."""

    def __init__(self, generation: int = 1) -> None:
        self.generation = generation

    @property
    def current(self):
        return self

    def frame_to_screen(self, fx, fy, fw, fh):
        return int(100 + fx), int(50 + fy)

    def contains_screen_point(self, sx, sy):
        return 100 <= sx < 2020 and 50 <= sy < 1130


class MappedTracker(FakeTracker):
    def __init__(self) -> None:
        self._geom = MappedGeometry()

    def move(self) -> None:
        self._geom = MappedGeometry(self._geom.generation + 1)


@pytest.fixture
def mapped():
    capture, tracker, executor = FakeCapture(), MappedTracker(), FakeExecutor()
    arbiter = Arbiter(capture, tracker, executor)
    capture.frames.frame = FakeFrame(1, 1, time.monotonic())
    _, obs = arbiter.observe()
    return arbiter, obs, tracker


# --- buttons and the wheel ---------------------------------------------------


@pytest.mark.parametrize("name,button", [("x1", Button.X1), ("mouse4", Button.X1),
                                         ("X2", Button.X2), ("mouse5", Button.X2),
                                         (" Middle ", Button.MIDDLE)])
def test_side_buttons_have_names(name, button):
    assert button_from_name(name) is button


@pytest.mark.parametrize("name", ["x3", "mouse6", "", "leftt", None])
def test_an_unknown_button_is_refused_not_read_as_left(name):
    with pytest.raises(ValueError, match="unknown button"):
        button_from_name(name)


@pytest.mark.parametrize("button,data", [(Button.X1, 1), (Button.X2, 2)])
def test_side_button_events_carry_which_button_in_mouse_data(button, data):
    """The two side buttons share their flags; only mouseData tells Windows
    which one. A zero there is a press of neither."""
    down, up = button.event(up=False), button.event(up=True)
    assert down.mi.dwFlags == MOUSEEVENTF_XDOWN and down.mi.mouseData == data
    assert up.mi.dwFlags == MOUSEEVENTF_XUP and up.mi.mouseData == data


def test_scroll_is_clamped_and_must_be_whole():
    assert parse([{"do": "scroll", "clicks": 99}]) == [Scroll(MAX_SCROLL_CLICKS)]
    assert parse([{"do": "scroll", "clicks": -99}]) == [Scroll(-MAX_SCROLL_CLICKS)]
    assert parse([{"do": "scroll", "clicks": 2.0, "horizontal": True}]) == [Scroll(2, True)]


def test_scroll_action(mapped):
    arbiter, obs, _ = mapped
    action = arbiter.scroll_action(obs, -3)
    assert action.steps == [Scroll(-3, False)]
    with pytest.raises(ValueError, match="must not be 0"):
        arbiter.scroll_action(obs, 0)


class DataRecorder:
    """Stands in for _send and keeps what the mouse half of each event said."""

    def __init__(self) -> None:
        self.mouse: list[tuple[int, int]] = []
        self.lock = threading.Lock()

    def __call__(self, events) -> None:
        with self.lock:
            for ev in events:
                if ev.type == gl_input.INPUT_MOUSE:
                    self.mouse.append((ev.mi.dwFlags, ev.mi.mouseData))


def _run(live, monkeypatch, steps):
    sup, ex = live
    rec = DataRecorder()
    monkeypatch.setattr(gl_input, "_send", rec)
    done, outcome = threading.Event(), []
    ex.submit(gl_input.Sequence(steps=steps, on_outcome=lambda o: (outcome.append(o),
                                                                    done.set())))
    assert done.wait(2.0)
    assert outcome[0].status == "sent", outcome[0]
    return rec, ex


def test_the_executor_sends_a_negative_scroll_as_its_twos_complement(live, monkeypatch):
    rec, _ = _run(live, monkeypatch, [Scroll(-2), Scroll(3, True)])
    assert rec.mouse == [(MOUSEEVENTF_WHEEL, (-240) & 0xFFFFFFFF),
                         (MOUSEEVENTF_HWHEEL, 360)]


def test_a_held_side_button_is_released_with_its_own_mouse_data(live, monkeypatch):
    """release_all goes through Button.event too: an X2 release sent as X1 would
    leave X2 held in the game."""
    rec, ex = _run(live, monkeypatch, [ButtonDown(Button.X2)])
    assert rec.mouse == [(MOUSEEVENTF_XDOWN, 2)]
    ex.release_all("test")
    assert rec.mouse[-1] == (MOUSEEVENTF_XUP, 2)
    assert ex.snapshot()["pressed_buttons"] == []


# --- pointing inside a sequence ----------------------------------------------


def test_a_drag_is_move_press_move_release():
    steps = parse([{"do": "move", "x": 10, "y": 20}, {"do": "button_down"},
                   {"do": "move", "x": 300, "y": 20}, {"do": "button_up"}])
    assert steps == [PointAt(10.0, 20.0), ButtonDown(Button.LEFT),
                     PointAt(300.0, 20.0), ButtonUp(Button.LEFT)]
    assert sequence_points(steps) == [(10.0, 20.0), (300.0, 20.0)]


def test_a_click_step_with_a_point_moves_settles_and_taps():
    steps = parse([{"do": "key_down", "key": "shift"},
                   {"do": "click", "x": 5, "y": 6, "button": "right"}])
    assert steps[1:] == [PointAt(5.0, 6.0), Dwell(CLICK_SETTLE), ButtonDown(Button.RIGHT),
                         Dwell(CLICK_HOLD), ButtonUp(Button.RIGHT), gl_input.KeyUp(key_code("shift"))]
    assert steps[0] == KeyDown(key_code("shift"))


def test_a_click_step_without_a_point_presses_where_the_cursor_is():
    assert parse([{"do": "click", "button": "mouse4"}]) == [
        ButtonDown(Button.X1), Dwell(CLICK_HOLD), ButtonUp(Button.X1)]


def test_a_hover_alone_is_a_sequence():
    """A move presses nothing, but it is new input -- it goes through the
    interlocks and the rate limiter like any press."""
    assert parse([{"do": "move", "x": 1, "y": 1}]) == [PointAt(1.0, 1.0)]
    with pytest.raises(ValueError, match="rate limiter"):
        parse([{"do": "move", "x": i, "y": 1} for i in range(5)], capacity=4)


def test_points_become_screen_moves_through_the_window(mapped):
    arbiter, obs, _ = mapped
    steps = parse([{"do": "move", "x": 10, "y": 20}, {"do": "click", "x": 30, "y": 40}])
    action = arbiter.sequence_action(obs, steps)
    moves = [s for s in action.steps if isinstance(s, MoveTo)]
    assert moves == [MoveTo(110, 70), MoveTo(130, 90)]
    assert not any(isinstance(s, PointAt) for s in action.steps)


def test_one_point_outside_the_frame_refuses_the_whole_sequence(mapped):
    """A drag whose end is off the window would release the button over
    whatever is next to the game."""
    arbiter, obs, _ = mapped
    steps = parse([{"do": "move", "x": 10, "y": 20}, {"do": "button_down"},
                   {"do": "move", "x": 5000, "y": 20}])
    with pytest.raises(ActionRejected) as exc:
        arbiter.sequence_action(obs, steps)
    assert exc.value.reason is Rejection.OUT_OF_BOUNDS


def test_points_are_not_mapped_through_a_window_that_moved(mapped):
    arbiter, obs, tracker = mapped
    tracker.move()
    with pytest.raises(ActionRejected) as exc:
        arbiter.sequence_action(obs, parse([{"do": "move", "x": 1, "y": 1}]))
    assert exc.value.reason is Rejection.GEOMETRY_MOVED


def test_a_sequence_without_points_needs_no_geometry(mapped):
    arbiter, obs, tracker = mapped
    tracker.move()          # would refuse any point; there are none
    action = arbiter.sequence_action(obs, parse([{"do": "tap", "key": "tab"}]))
    assert action.steps[0] == KeyDown(key_code("tab"))


# --- rebinding a sequence that points ------------------------------------------


def test_a_rebound_drag_needs_every_point_unchanged(lens):
    """The grab point survived; the drop point did not. The drop is where the
    item lands, so this must refuse -- checking only the first point would not."""
    token = shown(lens)
    changed = scene()
    changed[140:210, 440:560, :3] = 255 - changed[140:210, 440:560, :3]   # B3
    later(lens, changed)
    got = GameLens._bind(lens, token, "t", True, points=[BUTTON, (500, 175)])
    assert isinstance(got, Dispatch) and got.verdict == "SCREEN_CHANGED"
    assert got.to_dict()["points"] == 2


def test_a_rebound_drag_over_an_unchanged_screen_moves_to_the_new_frame(lens):
    token = shown(lens)
    later(lens)
    obs, binding = GameLens._bind(lens, token, "t", True, points=[BUTTON, (500, 175)])
    assert binding["bound_to"] == "fresh" and binding["patch_max"] == 0


def test_submit_sequence_checks_its_points_when_rebinding(lens):
    """The rule above is only worth anything if the sequence path hands its
    points to it. A changed drop point, through the real entry point."""
    token = shown(lens)
    changed = scene()
    changed[140:210, 440:560, :3] = 255 - changed[140:210, 440:560, :3]
    later(lens, changed)
    steps = parse([{"do": "move", "x": BUTTON[0], "y": BUTTON[1]}, {"do": "button_down"},
                   {"do": "move", "x": 500, "y": 175}])
    got = GameLens.submit_sequence(lens, observation_id=token, steps=steps, rebind=True)
    assert got.verdict == "SCREEN_CHANGED"


def test_a_point_that_cannot_be_compared_refuses(lens):
    token = shown(lens)
    later(lens)
    got = GameLens._bind(lens, token, "t", True, points=[BUTTON, (-5, 175)])
    assert isinstance(got, Dispatch) and got.verdict == "SCREEN_CHANGED"


# --- at the HTTP edge ---------------------------------------------------------


@pytest.fixture
def http():
    from fastapi.testclient import TestClient

    from gamelens.server import create_app
    from tests.test_http_sequence import SeqRuntime

    class ScrollRuntime(SeqRuntime):
        def submit_scroll(self, **kwargs) -> Dispatch:
            self.submitted.append(("scroll", kwargs))
            return self.next_dispatch

    runtime = ScrollRuntime()
    return TestClient(create_app(runtime), base_url="http://127.0.0.1:8777"), runtime


def _act(http, body):
    from tests.test_http_auth import agent

    client, runtime = http
    return client.post("/act", headers=agent(runtime), json={"observation_id": "obs", **body})


def test_scroll_reaches_the_runtime(http):
    r = _act(http, {"kind": "scroll", "clicks": -3, "horizontal": True})
    assert r.status_code == 200, r.text
    kind, kwargs = http[1].submitted[-1]
    assert kind == "scroll" and kwargs["clicks"] == -3 and kwargs["horizontal"] is True


@pytest.mark.parametrize("body", [{"kind": "scroll"}, {"kind": "scroll", "clicks": 0},
                                  {"kind": "scroll", "clicks": 1.5},
                                  {"kind": "scroll", "clicks": "2"},
                                  {"kind": "scroll", "clicks": 1, "horizontal": "false"}])
def test_a_bad_scroll_is_a_400_before_anything_is_queued(http, body):
    r = _act(http, body)
    assert r.status_code == 400, r.text
    assert http[1].submitted == []


def test_a_sequence_that_drags_reaches_the_runtime_with_its_points(http):
    r = _act(http, {"kind": "sequence", "steps": [
        {"do": "move", "x": 1, "y": 2}, {"do": "button_down", "button": "mouse5"},
        {"do": "move", "x": 3, "y": 4}]})
    assert r.status_code == 200, r.text
    steps = http[1].submitted[-1][1]["steps"]
    assert sequence_points(steps) == [(1.0, 2.0), (3.0, 4.0)]
    assert steps[-1] == ButtonUp(Button.X2)


def test_the_mcp_schema_offers_scroll():
    from gamelens.mcp import ACTION_SCHEMA

    assert "scroll" in ACTION_SCHEMA["properties"]["kind"]["enum"]


# --- GL040 inspection 1: per-press guards ---------------------------------------


def _outcome(live, monkeypatch, steps):
    sup, ex = live
    rec = DataRecorder()
    monkeypatch.setattr(gl_input, "_send", rec)
    done, outcome = threading.Event(), []
    ex.submit(gl_input.Sequence(steps=steps, on_outcome=lambda o: (outcome.append(o),
                                                                    done.set())))
    assert done.wait(2.0)
    deadline = time.monotonic() + 2.0
    while (ex.snapshot()["pressed_keys"] or ex.snapshot()["pressed_buttons"]) \
            and time.monotonic() < deadline:
        time.sleep(0.005)
    return outcome[0], rec, ex


@pytest.mark.parametrize("step", [ButtonDown(Button.LEFT), Scroll(1)])
def test_a_press_or_scroll_with_the_cursor_off_the_game_is_refused(live, monkeypatch, step):
    """I01: a look can walk an unlocked cursor off the window; a press there
    would land on whatever is next to the game while it is still foreground."""
    monkeypatch.setattr(gl_input, "pointer_on_window", lambda hwnd: False)
    out, rec, _ = _outcome(live, monkeypatch, [step])
    assert out.status == "denied" and out.detail == "the cursor is not over the target window"
    assert rec.mouse == []


def test_look_then_press_off_the_window_sends_the_look_but_not_the_press(live, monkeypatch):
    sup, _ = live
    where = {"on": True}
    monkeypatch.setattr(gl_input, "pointer_on_window", lambda hwnd: where["on"])
    real_apply = gl_input.InputExecutor._apply_new

    def apply(self, step):
        real_apply(self, step)
        if isinstance(step, gl_input.LookBy):
            where["on"] = False            # the look carried the cursor off

    monkeypatch.setattr(gl_input.InputExecutor, "_apply_new", apply)
    out, rec, _ = _outcome(live, monkeypatch, [gl_input.LookBy(600, 0), ButtonDown(Button.LEFT)])
    assert out.status == "denied"
    assert [f for f, _ in rec.mouse] == [gl_input.MOUSEEVENTF_MOVE]


@pytest.mark.parametrize("held,key", [({0xA4}, "f4"), ({0xA5}, "tab"), ({0x5B}, "d"),
                                      ({0xA2}, "escape")])
def test_a_key_is_refused_while_the_owner_holds_a_chord_modifier(live, monkeypatch, held, key):
    """I03: Alt held on the physical keyboard plus an allowed F4 is Alt+F4."""
    monkeypatch.setattr(gl_input, "held_keys", lambda vks: held & set(vks))
    out, rec, ex = _outcome(live, monkeypatch, [KeyDown(key_code(key))])
    assert out.status == "denied" and out.detail == "a modifier GameLens did not press is held down"


def test_ctrl_held_by_someone_else_does_not_block_ordinary_keys(live, monkeypatch):
    monkeypatch.setattr(gl_input, "held_keys", lambda vks: {0xA2} & set(vks))
    out, _, _ = _outcome(live, monkeypatch, [KeyDown(key_code("w"))])
    assert out.status == "sent"


def test_a_modifier_the_harness_pressed_itself_is_not_foreign(live, monkeypatch):
    """GetAsyncKeyState also sees injected keys, so ctrl the executor itself
    holds must not count as someone else's. (Steps built directly: a parsed
    sequence cannot contain escape at all.)"""
    monkeypatch.setattr(gl_input, "held_keys", lambda vks: {0xA2} & set(vks))
    ctrl = key_code("ctrl")
    out, _, _ = _outcome(live, monkeypatch, [KeyDown(ctrl), KeyDown(0x1B)])
    assert out.status == "sent"


def test_a_failed_release_stops_the_sequence_and_blocks_new_input(live, monkeypatch):
    """I02: a key-up SendInput rejected used to be ignored -- the sequence
    reported 'sent' and the next action pressed on top of the stuck key."""
    sup, ex = live
    fail = {"on": True}
    sent = []

    def send(events):
        for ev in events:
            if ev.type == gl_input.INPUT_KEYBOARD and ev.ki.dwFlags & gl_input.KEYEVENTF_KEYUP \
                    and fail["on"]:
                raise gl_input.InjectionFailed("rejected")
            sent.append(ev.type)

    monkeypatch.setattr(gl_input, "_send", send)

    def run(steps):
        done, outcome = threading.Event(), []
        ex.submit(gl_input.Sequence(steps=steps, on_outcome=lambda o: (outcome.append(o),
                                                                        done.set())))
        assert done.wait(2.0)
        return outcome[0]

    alt = key_code("alt")
    first = run([KeyDown(alt), gl_input.KeyUp(alt)])
    assert first.status == "error" and "release" in first.detail
    deadline = time.monotonic() + 2.0
    while not ex.unreleased and time.monotonic() < deadline:
        time.sleep(0.005)
    assert ex.unreleased

    before = len(sent)
    second = run([KeyDown(key_code("f4"))])
    assert second.status == "denied"
    assert second.detail == "an earlier release failed; input may still be held"
    assert len(sent) == before, "F4 must not be sent while alt may be stuck"

    fail["on"] = False                     # the release works again
    third = run([KeyDown(key_code("w"))])
    assert third.status == "sent" and not ex.unreleased
