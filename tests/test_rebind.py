"""GL-039: carrying an agent's decision over to a newer frame, and when not to.

An agent acting through a tool looks at an image, spends a model turn deciding,
and only then acts -- by which time the arbiter's 0.8s limit has nearly always
passed. Refusing all of those makes the tool useless; accepting them on the
newest frame makes it unsafe. `GameLens._bind` sits between: age may have
changed, nothing else may have, and a click also needs the pixels at the click
point to be the ones the agent saw.

The cases that matter are the ones a naive rebind passes: an observation that
aged out *and* was preempted (evaluate() reports only the first rule it
breaks), and a click target that changed in a way too small to move a
whole-screen score.
"""

from __future__ import annotations

import time

import cv2
import numpy as np
import pytest

from gamelens.app import (
    GLOBAL_MAD,
    PATCH_HALF,
    PATCH_MAX_DIFF,
    PATCH_MEAN_DIFF,
    Dispatch,
    GameLens,
    ObservationRegistry,
    same_at_click,
)
from gamelens.arbiter import Arbiter
from gamelens.server import ActionLog, Encoded, encode_jpeg
from tests.test_arbiter import FakeBackend, FakeCapture, FakeExecutor, FakeTracker

W, H = 640, 360
LIMIT = 0.05            # the arbiter's age limits in these tests, seconds


def scene() -> np.ndarray:
    """A BGRA frame that looks like a menu: flat panels and a few buttons."""
    img = np.zeros((H, W, 4), np.uint8)
    img[:, :, :3] = (60, 60, 60)
    cv2.rectangle(img, (40, 40), (600, 320), (110, 110, 110, 255), -1)
    for i, x in enumerate(range(80, 560, 120)):
        cv2.rectangle(img, (x, 150), (x + 90, 200), (150 + 20 * i, 140, 90, 255), -1)
        cv2.putText(img, f"B{i}", (x + 25, 185), cv2.FONT_HERSHEY_SIMPLEX, 0.8,
                    (240, 240, 240, 255), 2)
    return img


class ArrFrame:
    def __init__(self, frame_id: int, array: np.ndarray, session_id: int = 1) -> None:
        self.frame_id = frame_id
        self.session_id = session_id
        self.captured_at = time.monotonic()
        self.array = array
        self.height, self.width = array.shape[:2]

    def release(self) -> None:
        pass


@pytest.fixture
def lens():
    capture, tracker, executor = FakeCapture(), FakeTracker(), FakeExecutor()
    lens = object.__new__(GameLens)
    lens.capture = capture
    lens.geometry = tracker
    lens.arbiter = Arbiter(capture, tracker, executor,
                           observation_deadline=LIMIT, action_ttl=LIMIT)
    lens.observations = ObservationRegistry()
    lens.log = ActionLog()
    capture.frames.frame = ArrFrame(1, scene())
    return lens


def shown(lens, quality: int = 50) -> str:
    """Hand an image out, as /frame.jpg does, and return its observation id."""
    result = GameLens.encode_frame(lens, quality)
    assert isinstance(result, Encoded)
    return result.observation_id


def later(lens, array: np.ndarray | None = None, session_id: int = 1) -> None:
    """Let the shown observation age out, then publish a newer frame."""
    time.sleep(LIMIT * 2.5)
    lens.capture.frames.frame = ArrFrame(
        lens.capture.frames.frame.frame_id + 1,
        scene() if array is None else array, session_id)


BUTTON = (125, 175)     # centre of B0, in image pixels


# --- when rebinding is allowed ----------------------------------------------


def test_a_fresh_observation_is_used_as_it_is(lens):
    token = shown(lens)
    obs, binding = GameLens._bind(lens, token, "t", True, points=[BUTTON])
    assert binding == {"bound_to": "shown"}
    assert obs is lens.observations.resolve(token)


def test_without_rebind_nothing_changes(lens):
    token = shown(lens)
    later(lens)
    obs, binding = GameLens._bind(lens, token, "t", False, points=[BUTTON])
    assert binding is None and obs is lens.observations.resolve(token)


def test_an_expired_click_on_an_unchanged_screen_moves_to_the_new_frame(lens):
    token = shown(lens)
    later(lens)
    obs, binding = GameLens._bind(lens, token, "t", True, points=[BUTTON])
    assert binding["bound_to"] == "fresh"
    assert obs.frame_id == 2 and binding["frame"] == 2 and binding["shown_frame"] == 1
    assert binding["patch_max"] == 0 and binding["global_mad"] == 0.0
    assert lens.arbiter.is_fresh(obs)


def test_an_expired_key_moves_to_the_new_frame_even_when_the_screen_moved(lens):
    token = shown(lens)
    moved = scene()
    moved[:, :, :3] = 255 - moved[:, :, :3]        # everything changed
    later(lens, moved)
    obs, binding = GameLens._bind(lens, token, "t", True)
    assert binding["bound_to"] == "fresh" and obs.frame_id == 2


# --- GL039-R2: age is the only thing allowed to have changed ------------------


def test_expired_and_preempted_is_refused_as_preempted(lens):
    """evaluate() would answer EXPIRED here and never get to PREEMPTED, so a
    rebind that only checked the fresh record would launder the preemption."""
    token = shown(lens)
    lens.arbiter.preempt("a reflex acted")
    later(lens)
    got = GameLens._bind(lens, token, "t", True, points=[BUTTON])
    assert isinstance(got, Dispatch)
    assert got.verdict == "PREEMPTED" and got.outcome == "denied"
    assert got.to_dict()["bound_to"] is None


def test_expired_and_preempted_key_is_refused_too(lens):
    token = shown(lens)
    lens.arbiter.preempt("a reflex acted")
    later(lens)
    got = GameLens._bind(lens, token, "t", True)
    assert isinstance(got, Dispatch) and got.verdict == "PREEMPTED"


def test_expired_and_backend_replaced_is_refused(lens):
    token = shown(lens)
    lens.capture.backend = FakeBackend(session_id=2)
    later(lens, session_id=2)
    got = GameLens._bind(lens, token, "t", True)
    assert isinstance(got, Dispatch) and got.verdict == "RETIRED_SESSION"


def test_a_newest_frame_from_another_session_is_refused(lens):
    """The backend on record still claims the old session, but the newest frame
    was published by a different one -- a late frame across a failover. The
    records disagree, and that alone must refuse."""
    token = shown(lens)
    later(lens, session_id=2)
    got = GameLens._bind(lens, token, "t", True)
    assert isinstance(got, Dispatch) and got.verdict == "RETIRED_SESSION"


def test_expired_and_backend_retired_is_refused(lens):
    token = shown(lens)
    lens.capture.backend.retired = True
    later(lens)
    got = GameLens._bind(lens, token, "t", True)
    assert isinstance(got, Dispatch) and got.verdict == "RETIRED_SESSION"


def test_expired_and_window_moved_is_refused(lens):
    token = shown(lens)
    lens.geometry.move()
    later(lens)
    got = GameLens._bind(lens, token, "t", True, points=[BUTTON])
    assert isinstance(got, Dispatch) and got.verdict == "GEOMETRY_MOVED"


def test_expired_and_resized_is_refused(lens):
    token = shown(lens)
    later(lens, cv2.resize(scene(), (W - 40, H - 20)))
    got = GameLens._bind(lens, token, "t", True, points=[BUTTON])
    assert isinstance(got, Dispatch) and got.verdict == "GEOMETRY_MOVED"


def test_a_record_that_is_gone_fails_closed(lens):
    lens.observations = ObservationRegistry(ttl=LIMIT)
    token = shown(lens)
    later(lens)
    time.sleep(LIMIT)
    got = GameLens._bind(lens, token, "t", True)
    assert isinstance(got, Dispatch) and got.verdict == "STALE_OBSERVATION"


def test_an_evicted_record_fails_closed(lens):
    lens.observations = ObservationRegistry(capacity=1)
    token = shown(lens)
    shown(lens)                                  # evicts the first
    got = GameLens._bind(lens, token, "t", True)
    assert isinstance(got, Dispatch) and got.verdict == "STALE_OBSERVATION"


def test_no_frame_to_rebind_to_is_refused(lens):
    token = shown(lens)
    time.sleep(LIMIT * 2.5)
    lens.capture.frames.frame = None
    got = GameLens._bind(lens, token, "t", True)
    assert isinstance(got, Dispatch) and got.verdict == "NO_FRAME"


# --- GL039-R3: the click point itself must be unchanged ------------------------


def _changed_at(x: int, y: int, size: int = 12, colour=(20, 220, 20)) -> np.ndarray:
    img = scene()
    img[y - size // 2:y + size // 2, x - size // 2:x + size // 2, :3] = colour
    return img


def test_a_small_change_at_the_click_point_refuses_the_click(lens):
    """12x12 of 640x360 moves the whole-screen score by well under its limit --
    the case a global comparison alone would wave through."""
    token = shown(lens)
    changed = _changed_at(*BUTTON)
    later(lens, changed)
    got = GameLens._bind(lens, token, "t", True, points=[BUTTON])
    assert isinstance(got, Dispatch) and got.verdict == "SCREEN_CHANGED"
    body = got.to_dict()
    assert body["global_mad"] < GLOBAL_MAD, "this change must be invisible globally"
    assert body["patch_max"] > 12


def test_the_same_change_away_from_the_click_is_allowed(lens):
    """The residual risk, pinned: a change outside the patch that does not move
    the global score is not seen. Here it is 60px from the click."""
    token = shown(lens)
    later(lens, _changed_at(BUTTON[0] + 60 + PATCH_HALF, BUTTON[1]))
    obs, binding = GameLens._bind(lens, token, "t", True, points=[BUTTON])
    assert binding["bound_to"] == "fresh"


def test_a_colour_only_change_at_the_click_point_is_refused(lens):
    """Same greyscale, different colour: a grey comparison would call it equal."""
    base = scene()
    x, y = BUTTON
    # Larger than the patch, so no edge of the block -- where JPEG rings and the
    # greyscale difference would jump -- falls inside the comparison window.
    r = 30
    base[y - r:y + r, x - r:x + r, :3] = (0, 0, 255)           # BGR red
    grey = int(cv2.cvtColor(np.uint8([[[0, 0, 255]]]), cv2.COLOR_BGR2GRAY)[0, 0])
    alt = base.copy()
    # A green with the same luma as that red (luma = 0.587 G for pure green).
    alt[y - r:y + r, x - r:x + r, :3] = (0, int(round(grey / 0.587)), 0)
    # Prove the fixture: in grey, as delivered, this change would pass the patch.
    ja = cv2.imdecode(np.frombuffer(encode_jpeg(base, 50)[0], np.uint8), cv2.IMREAD_GRAYSCALE)
    jb = cv2.imdecode(np.frombuffer(encode_jpeg(alt, 50)[0], np.uint8), cv2.IMREAD_GRAYSCALE)
    grey_patch = np.abs(ja.astype(int) - jb.astype(int))[y - PATCH_HALF:y + PATCH_HALF + 1,
                                                         x - PATCH_HALF:x + PATCH_HALF + 1]
    assert grey_patch.max() <= PATCH_MAX_DIFF and grey_patch.mean() <= PATCH_MEAN_DIFF, (
        "the fixture must be a colour-only change")

    lens.capture.frames.frame = ArrFrame(1, base)
    token = shown(lens)
    later(lens, alt)
    got = GameLens._bind(lens, token, "t", True, points=[BUTTON])
    assert isinstance(got, Dispatch) and got.verdict == "SCREEN_CHANGED"


def test_a_whole_screen_change_refuses_the_click_even_if_the_point_survived(lens):
    token = shown(lens)
    img = scene()
    x, y = BUTTON
    keep = img[y - 20:y + 20, x - 20:x + 20].copy()
    img[:, :, :3] = 255 - img[:, :, :3]
    img[y - 20:y + 20, x - 20:x + 20] = keep
    later(lens, img)
    got = GameLens._bind(lens, token, "t", True, points=[BUTTON])
    assert isinstance(got, Dispatch) and got.verdict == "SCREEN_CHANGED"
    assert got.to_dict()["global_mad"] > GLOBAL_MAD


def test_a_click_rebind_without_the_shown_image_fails_closed(lens):
    obs = lens.arbiter.observation_for(lens.capture.frames.frame, scale=1.0)
    token = lens.observations.issue(obs)             # no jpeg on record
    later(lens)
    got = GameLens._bind(lens, token, "t", True, points=[BUTTON])
    assert isinstance(got, Dispatch) and got.verdict == "STALE_OBSERVATION"


# --- same_at_click on its own -------------------------------------------------


def test_identical_images_score_zero():
    jpeg, _ = encode_jpeg(scene(), quality=50)
    got = same_at_click(jpeg, jpeg, *BUTTON)
    assert got == {"patch_max": 0, "patch_mean": 0.0, "global_mad": 0.0, "ok": True}


@pytest.mark.parametrize("junk", [b"", b"not a jpeg"])
def test_undecodable_images_answer_no(junk):
    jpeg, _ = encode_jpeg(scene(), quality=50)
    assert same_at_click(jpeg, junk, *BUTTON)["ok"] is False
    assert same_at_click(junk, jpeg, *BUTTON)["ok"] is False


@pytest.mark.parametrize("x,y", [(-1, 10), (10, -1), (W, 10), (10, H)])
def test_a_click_outside_the_image_answers_no(x, y):
    jpeg, _ = encode_jpeg(scene(), quality=50)
    assert same_at_click(jpeg, jpeg, x, y)["ok"] is False


def test_the_patch_is_clipped_at_the_edge():
    jpeg, _ = encode_jpeg(scene(), quality=50)
    assert same_at_click(jpeg, jpeg, 0, 0)["ok"] is True
