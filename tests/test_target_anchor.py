"""Target identity survives animation; changed labels/provenance still refuse."""
import cv2
import numpy as np
import pytest

from gamelens.app import Dispatch, GameLens
from gamelens.server import encode_jpeg
from gamelens.target import parse_anchor, same_at_anchor
from tests.test_rebind import lens, shown, later, scene

ANCHOR = dict(x=80, y=150, width=100, height=30, color="yellow")
POINT = (125, 190)


def labeled(text="Trainer1", bg=40):
    image = scene()
    image[150:180, 80:180, :3] = bg
    cv2.putText(image, text, (82, 172), cv2.FONT_HERSHEY_SIMPLEX, .45,
                (0, 255, 255, 255), 1)
    return image


def jpg(image):
    return encode_jpeg(image, quality=90)[0]


def test_animation_behind_label_and_click_is_allowed():
    a, b = labeled(), labeled(bg=75)
    b[180:205, 100:150, :3] = (220, 15, 170)
    assert same_at_anchor(jpg(a), jpg(b), ANCHOR, *POINT)["anchor_ok"]


@pytest.mark.parametrize("change", ["text", "hidden", "moved", "flat", "size"])
def test_missing_changed_or_uninformative_target_refuses(change):
    a, b = labeled(), labeled()
    if change == "text": b = labeled("Trainer2")
    if change == "hidden": b[150:180, 80:180, :3] = 40
    if change == "moved": b = np.roll(b, 6, axis=1)
    if change == "flat": a[150:180, 80:180, :3] = (0, 255, 255)
    if change == "size": b = b[:, :-1]
    assert not same_at_anchor(jpg(a), jpg(b), ANCHOR, *POINT)["anchor_ok"]


@pytest.mark.parametrize("anchor,point", [
    ({**ANCHOR, "color": "white"}, POINT),
    ({**ANCHOR, "x": True}, POINT),
    ({**ANCHOR, "height": 80}, POINT),
    ({**ANCHOR, "width": float("nan")}, POINT),
    ({**ANCHOR, "extra": 1}, POINT),
    (ANCHOR, (400, 300)),
])
def test_bad_anchor_rejected_before_input(anchor, point):
    with pytest.raises(ValueError): parse_anchor(anchor, *point)


@pytest.mark.parametrize("change,verdict", [
    ("none", None), ("label", "TARGET_CHANGED"),
    ("geometry", "GEOMETRY_MOVED"), ("preempt", "PREEMPTED"),
    ("session", "RETIRED_SESSION"),
])
def test_anchor_binding_keeps_all_provenance_guards(lens, change, verdict):
    from tests.test_rebind import ArrFrame
    lens.capture.frames.frame = ArrFrame(1, labeled())
    token = shown(lens, quality=90)
    later(lens, labeled("Trainer2" if change == "label" else "Trainer1", bg=75),
          session_id=2 if change == "session" else 1)
    if change == "geometry": lens.geometry.move()
    if change == "preempt": lens.arbiter.preempt()
    result = GameLens._bind(lens, token, "npc", True, points=[POINT], anchor=ANCHOR)
    if verdict:
        assert isinstance(result, Dispatch) and result.verdict == verdict
    else:
        assert result[1]["bound_to"] == "anchored"


def test_expired_record_cannot_be_anchored(lens):
    assert GameLens._bind(lens, "gone", "npc", True, points=[POINT], anchor=ANCHOR).verdict == "STALE_OBSERVATION"


def test_mouse_movement_inside_target_stops_click(live, monkeypatch):
    import threading
    from gamelens import input as gl_input
    from gamelens.input import MoveTo, ButtonDown, ButtonUp, Sequence, Dwell
    from gamelens.safety import Denial
    events, outcomes, done = [], [], threading.Event()
    monkeypatch.setattr(gl_input, "_send", lambda items: events.extend(items))
    monkeypatch.setattr(gl_input, "pointer_on_window", lambda hwnd: True)
    monkeypatch.setattr(gl_input, "cursor_position", lambda: (30, 40))
    live[1].submit(Sequence(steps=[MoveTo(10, 20), Dwell(.01), ButtonDown(), ButtonUp()],
                           on_outcome=lambda o: (outcomes.append(o), done.set())))
    assert done.wait(2)
    assert outcomes[0].status == "denied"
    assert outcomes[0].partial and outcomes[0].injected_steps == 1
    assert "do not replay" in outcomes[0].detail
    assert Denial.POINTER_MOVED.value in outcomes[0].detail
    assert not any(e.mi.dwFlags & gl_input.MOUSEEVENTF_LEFTDOWN for e in events if e.type == gl_input.INPUT_MOUSE)


from tests.test_killswitch import hwnd, live
