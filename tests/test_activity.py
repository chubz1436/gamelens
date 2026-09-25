"""GL-042: the whole-screen rebind score ignores what was already moving.

Bedrock's Play screen draws unchanged buttons over a moving panorama. The old
whole-image score (6.5-14 over 1-10 s, limit 2.0) refused every late click on
it. And the score cannot simply be loosened: the Settings and Play screens put
an identical back arrow at the identical spot, and the whole-screen score is
the only thing that tells those apart. These tests pin both halves: animation
that was already going on is ignored, a swap is not, and a screen with nothing
moving is scored exactly as it was before.
"""

from __future__ import annotations

import time

import cv2
import numpy as np

from gamelens.activity import ACTIVITY_TILE, ActivityMap
from gamelens.app import GLOBAL_MAD, MIN_STILL, Dispatch, GameLens, same_at_click
from gamelens.server import encode_jpeg
from tests.test_rebind import BUTTON, H, W, later, lens, scene, shown  # noqa: F401

T = ACTIVITY_TILE


def with_band(img: np.ndarray, phase: int, top: int = 0, height: int = 40) -> np.ndarray:
    """``img`` with a moving stripe pattern in a band -- a panorama, in effect."""
    out = img.copy()
    xs = (np.arange(W) + phase * 7) % 32 < 16
    out[top:top + height, :, :3] = np.where(xs[None, :, None], 230, 20).astype(np.uint8)
    return out


RUN = 45           # frames at 20 Hz: a little over the 2 s window


def feed_run(amap: ActivityMap, frames, start: float = 100.0, step: float = 0.05,
             session: int = 0):
    for i, f in enumerate(frames):
        amap.feed(f, i + 1, session, now=start + i * step)
    return start + (len(frames) - 1) * step


# --- ActivityMap ----------------------------------------------------------------


def test_a_still_screen_has_nothing_moving():
    amap = ActivityMap()
    now = feed_run(amap, [scene()] * 10)
    moving = amap.snapshot(now=now)
    assert moving is not None and not moving.any()


def test_no_history_means_no_map():
    amap = ActivityMap()
    assert amap.snapshot() is None
    amap.feed(scene(), 1)
    assert amap.snapshot() is None          # one frame: nothing to compare


def test_the_moving_band_is_marked_and_the_rest_is_not():
    amap = ActivityMap(dilate=0)
    now = feed_run(amap, [with_band(scene(), i) for i in range(RUN)])
    moving = amap.snapshot(now=now)
    assert moving[: 40 // T].all()
    assert not moving[80 // T:].any()


def test_dilation_widens_by_one_tile():
    frames = [with_band(scene(), i, top=160, height=16) for i in range(RUN)]
    rows = {}
    for d in (0, 1):
        amap = ActivityMap(dilate=d)
        now = feed_run(amap, frames)
        rows[d] = np.flatnonzero(amap.snapshot(now=now).any(axis=1)).tolist()
    assert rows[0] and rows[1] == list(range(rows[0][0] - 1, rows[0][-1] + 2))


def test_motion_older_than_the_window_is_forgotten():
    amap = ActivityMap(window=1.0)
    frames = [with_band(scene(), i) for i in range(5)] + [scene()] * 31
    now = feed_run(amap, frames)             # 36 frames, 1.75 s
    assert not amap.snapshot(now=now).any()


def test_a_frame_id_seen_twice_counts_once():
    amap = ActivityMap()
    amap.feed(scene(), 1, 0, now=1.0)
    amap.feed(with_band(scene(), 3), 1, 0, now=1.05)   # same id: ignored
    amap.feed(scene(), 2, 0, now=1.1)
    assert not amap.snapshot(now=1.1).any()


def test_a_size_change_starts_the_history_again():
    amap = ActivityMap()
    feed_run(amap, [with_band(scene(), i) for i in range(5)])
    small = cv2.resize(scene(), (W // 2, H // 2))
    amap.feed(small, 99, 0, now=101.0)
    assert amap.snapshot(now=101.0) is None


# --- same_at_click with a map ----------------------------------------------------


def band_map(top=0, height=40) -> np.ndarray:
    amap = ActivityMap()
    now = feed_run(amap, [with_band(scene(), i, top, height) for i in range(RUN)])
    return amap.snapshot(now=now)


def jpg(img):
    return encode_jpeg(img, 50)[0]


def test_animation_that_was_already_going_on_is_ignored():
    a, b = jpg(with_band(scene(), 0)), jpg(with_band(scene(), 5))
    plain = same_at_click(a, b, *BUTTON)
    assert plain["global_mad"] > GLOBAL_MAD and not plain["ok"], "fixture must fail the old rule"
    got = same_at_click(a, b, *BUTTON, moving=band_map())
    assert got["ok"] and got["global_mad"] <= GLOBAL_MAD and got["still"] < 1.0


def test_a_swap_keeping_the_click_point_is_still_refused_under_a_map():
    """Settings -> Play: same back arrow, same place, different screen."""
    img = with_band(scene(), 5)
    x, y = BUTTON
    keep = img[y - 20:y + 20, x - 20:x + 20].copy()
    img[60:, :, :3] = 255 - img[60:, :, :3]
    img[y - 20:y + 20, x - 20:x + 20] = keep
    got = same_at_click(jpg(with_band(scene(), 0)), jpg(img), *BUTTON, moving=band_map())
    assert not got["ok"] and got["global_mad"] > GLOBAL_MAD


def test_a_change_only_where_things_were_moving_is_not_seen():
    """The residual risk, pinned: something new drawn entirely inside the
    moving area, away from the click, is not seen by the global score."""
    a = with_band(scene(), 0)
    b = with_band(scene(), 5)
    b[5:35, 300:400, :3] = (0, 0, 255)
    got = same_at_click(jpg(a), jpg(b), *BUTTON, moving=band_map())
    assert got["ok"]


def test_a_still_screen_is_scored_exactly_as_before():
    img = scene()
    other = img.copy()
    other[250:300, 300:600, :3] = 255 - other[250:300, 300:600, :3]
    a, b = jpg(img), jpg(other)
    none_moving = np.zeros((H // T, W // T), bool)
    assert same_at_click(a, b, *BUTTON, moving=none_moving) == same_at_click(a, b, *BUTTON)


def test_almost_everything_moving_falls_back_to_the_whole_screen():
    moving = np.ones((H // T, W // T), bool)
    moving[:2] = False                       # far under MIN_STILL still
    assert (~moving).mean() < MIN_STILL
    a, b = jpg(with_band(scene(), 0)), jpg(with_band(scene(), 5))
    got = same_at_click(a, b, *BUTTON, moving=moving)
    assert got["global_mad"] == same_at_click(a, b, *BUTTON)["global_mad"]
    assert not got["ok"]


def test_the_patch_is_never_masked():
    """A click on something that is moving must find it where it was."""
    top = BUTTON[1] - 20
    a = with_band(scene(), 0, top=top, height=40)
    b = with_band(scene(), 5, top=top, height=40)
    got = same_at_click(jpg(a), jpg(b), *BUTTON, moving=band_map(top=top, height=40))
    assert not got["ok"] and got["patch_max"] > 12


# --- end to end through _bind ------------------------------------------------------


def _animate(lens, frames=RUN):
    """Feed the map a band animating for the whole window, ending now, with ids
    below the frame about to be handed out, from its session."""
    t0 = time.monotonic() - (frames - 1) * 0.05
    for i in range(frames):
        lens.activity.feed(with_band(scene(), i), 100 + i, 1, now=t0 + i * 0.05)
    lens.capture.frames.frame = type(lens.capture.frames.frame)(
        200, with_band(scene(), frames), 1)


def test_a_late_click_on_an_animated_menu_is_rebound(lens):  # noqa: F811
    _animate(lens)
    token = shown(lens)
    later(lens, with_band(scene(), 20))
    obs, binding = GameLens._bind(lens, token, "t", True, points=[BUTTON])
    assert binding["bound_to"] == "fresh" and binding["still"] < 1.0


def test_the_same_late_click_without_a_map_is_refused(lens):  # noqa: F811
    lens.capture.frames.frame.array = with_band(scene(), 0)
    token = shown(lens)
    later(lens, with_band(scene(), 20))
    got = GameLens._bind(lens, token, "t", True, points=[BUTTON])
    assert isinstance(got, Dispatch) and got.verdict == "SCREEN_CHANGED"


def test_memory_is_bounded_by_the_window():
    amap = ActivityMap(window=1.0)
    feed_run(amap, [scene()] * 200)          # 10 s at 20 Hz
    assert len(amap._samples) <= 21


def test_drift_too_slow_between_samples_is_caught_across_the_window():
    """Live on the VM, translucent tabs over the Play screen's panorama drifted
    under the threshold per 50 ms sample and added up over seconds."""
    amap = ActivityMap(dilate=0)
    frames = []
    for i in range(RUN):
        f = scene()
        f[:40, :, :3] = 100 + i // 2            # +0.5 per sample on average
        frames.append(f)
    now = feed_run(amap, frames)
    moving = amap.snapshot(now=now)
    assert moving[: 40 // T].all() and not moving[80 // T:].any()


# --- GL042-I01: moving throughout, not moved once ----------------------------------


def test_a_burst_of_change_is_not_ongoing_motion():
    """A page transition: everything changes once, then nothing. Counting it as
    motion would hide the content a second swap replaces."""
    amap = ActivityMap()
    other = scene()
    other[:, :, :3] = 255 - other[:, :, :3]
    frames = [scene()] * 25 + [other] * 20     # instant swap 1.25 s in, then still
    now = feed_run(amap, frames)
    assert not amap.snapshot(now=now).any()


def test_motion_seen_only_in_part_of_the_window_is_not_enough():
    amap = ActivityMap()
    frames = [scene()] * 25 + [with_band(scene(), i) for i in range(20)]   # last 1 s
    now = feed_run(amap, frames)
    assert not amap.snapshot(now=now).any()


def test_a_short_history_marks_nothing():
    amap = ActivityMap()
    now = feed_run(amap, [with_band(scene(), i) for i in range(12)])      # 0.55 s
    assert not amap.snapshot(now=now).any()


# --- GL042-I02: the map belongs to the frame handed out ------------------------------


def test_samples_after_the_handed_out_frame_do_not_count():
    """The encoder leased frame F; the poller then fed a swap. The swap must not
    be in F's map."""
    amap = ActivityMap()
    now = feed_run(amap, [with_band(scene(), i) for i in range(RUN)])
    assert amap.snapshot(until_frame_id=RUN, now=now).any()
    assert not amap.snapshot(until_frame_id=RUN // 2, now=now).any()
    assert amap.snapshot(until_frame_id=1, now=now) is None       # one sample


def test_another_session_gets_no_map():
    amap = ActivityMap()
    now = feed_run(amap, [with_band(scene(), i) for i in range(RUN)], session=3)
    assert amap.snapshot(session_id=3, now=now).any()
    assert amap.snapshot(session_id=4, now=now) is None


def test_a_new_session_starts_the_history_again():
    amap = ActivityMap()
    feed_run(amap, [with_band(scene(), i) for i in range(RUN)], session=3)
    amap.feed(scene(), 999, 4, now=103.0)
    assert amap.snapshot(session_id=4, now=103.0) is None


def test_a_native_size_change_starts_again_even_with_the_same_tile_grid():
    amap = ActivityMap()
    feed_run(amap, [with_band(scene(), i) for i in range(RUN)])
    bigger = cv2.resize(scene(), (W + 4, H + 4))      # same W//16, H//16
    assert (bigger.shape[1] // T, bigger.shape[0] // T) == (W // T, H // T)
    amap.feed(bigger, 999, 0, now=103.0)
    assert amap.snapshot(now=103.0) is None


def test_encode_frame_takes_the_map_of_its_own_frame(lens):  # noqa: F811
    """End to end: samples fed after the handed-out frame's id are ignored."""
    _animate(lens)
    lens.capture.frames.frame = type(lens.capture.frames.frame)(
        50, with_band(scene(), 0), 1)                 # older than every sample fed
    token = shown(lens)
    assert lens.observations.record(token)[3] is None


def test_encode_frame_gets_no_map_from_another_session(lens):  # noqa: F811
    from tests.test_arbiter import FakeBackend

    _animate(lens)
    lens.capture.backend = FakeBackend(2)
    lens.capture.frames.frame = type(lens.capture.frames.frame)(
        300, with_band(scene(), 0), 2)                # session 2, map is session 1
    token = shown(lens)
    assert lens.observations.record(token)[3] is None


def test_the_poller_feeds_the_map_with_the_frame_session(lens):  # noqa: F811
    import threading
    from collections import deque

    lens._stop = threading.Event()
    lens._last_frame_id = 0
    lens._frame_times, lens._frame_ages = deque(maxlen=120), deque(maxlen=120)
    frame = type(lens.capture.frames.frame)(7, scene(), 5)
    frame.age = lambda: 0.0
    lens.capture.frames.frame = frame
    t = threading.Thread(target=GameLens._poll, args=(lens,), daemon=True)
    t.start()
    time.sleep(0.3)
    lens._stop.set()
    t.join(1.0)
    assert lens.activity._source is not None and lens.activity._source[0] == 5


# --- Codex round 2 -------------------------------------------------------------------


def test_something_that_has_just_stopped_is_not_moving():
    """GL042-RV02-I01: animating for most of the window, then a still menu for
    the last half second. Three old buckets must not mask it."""
    amap = ActivityMap()
    frames = [with_band(scene(), i) for i in range(33)] + [scene()] * 12
    now = feed_run(amap, frames)
    assert not amap.snapshot(now=now).any()


def test_a_map_from_another_native_size_is_not_used():
    """GL042-RV02-I02: the encoder can hand out the first frame of a new size
    before the poller has fed it."""
    amap = ActivityMap()
    now = feed_run(amap, [with_band(scene(), i) for i in range(RUN)])
    assert amap.snapshot(size=(H, W), now=now).any()
    assert amap.snapshot(size=(H + 4, W + 4), now=now) is None


def test_encode_frame_gets_no_map_for_a_frame_of_another_size(lens):  # noqa: F811
    _animate(lens)
    big = cv2.resize(with_band(scene(), 0), (W + 4, H + 4))
    lens.capture.frames.frame = type(lens.capture.frames.frame)(300, big, 1)
    token = shown(lens)
    assert lens.observations.record(token)[3] is None
