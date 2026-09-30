"""Render lag is not capture lag.

An action is acknowledged when it is injected, not when the game draws it, so
the frame that is newest the moment /act answers can still show the world
before the action. Callers used to cover that with a guessed sleep. These tests
are about the replacement: /act says which frame was newest when injection
finished, and /frame.jpg can be asked for a frame past it.

The ways it could be quietly wrong: handing back the stale frame anyway,
reporting an id for an action that never reached the game, naming a different
frame than the one that was encoded, and -- the one review caught -- waiting in
a way that starves /stop of the worker it is served from.
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from gamelens.activity import ActivityMap
from gamelens.app import Dispatch, GameLens
from gamelens.arbiter import Rejection
from gamelens.capture import Backend, Frame, FramePool, LatestFrame
from gamelens.input import Outcome
from gamelens.server import (ENCODE_FAILED, MAX_FRAME_WAITS, NO_FRAME, ActionLog,
                             Encoded, create_app)

from tests.test_http_auth import FakeRuntime, agent, op


# --- LatestFrame -------------------------------------------------------------


def _frame(pool: FramePool, frame_id: int) -> Frame:
    buf = pool.lease(4, 4)
    assert buf is not None
    return Frame(frame_id, 1, Backend.MSS, time.monotonic(), 0, 4, 4, buf, pool)


def test_latest_id_is_zero_before_anything_is_published():
    assert LatestFrame().latest_id() == 0


def test_acquire_at_least_returns_at_once_when_the_newest_already_qualifies():
    pool, slot = FramePool(), LatestFrame()
    slot.publish(_frame(pool, 12))
    started = time.monotonic()
    got = slot.acquire_at_least(12, timeout=1.0)
    assert got is not None and got.frame_id == 12
    assert time.monotonic() - started < 0.1
    got.release()


def test_acquire_at_least_waits_for_a_frame_published_later():
    pool, slot = FramePool(), LatestFrame()
    slot.publish(_frame(pool, 5))
    threading.Timer(0.05, lambda: slot.publish(_frame(pool, 6))).start()
    got = slot.acquire_at_least(6, timeout=1.0)
    assert got is not None and got.frame_id == 6
    got.release()


def test_acquire_at_least_never_hands_back_an_older_frame():
    """The stale picture is what the caller is trying to get past."""
    pool, slot = FramePool(), LatestFrame()
    slot.publish(_frame(pool, 5))
    started = time.monotonic()
    assert slot.acquire_at_least(6, timeout=0.1) is None
    assert time.monotonic() - started >= 0.09


def test_a_zero_or_negative_timeout_does_not_block():
    pool, slot = FramePool(), LatestFrame()
    slot.publish(_frame(pool, 1))
    started = time.monotonic()
    assert slot.acquire_at_least(2, timeout=0) is None
    assert slot.acquire_at_least(2, timeout=-5) is None
    assert time.monotonic() - started < 0.05


# --- _dispatch reports the frame that was newest when injection finished ------


class _Frames:
    def __init__(self, latest: int) -> None:
        self.latest = latest

    def latest_id(self) -> int:
        return self.latest

    def acquire(self, timeout=None):
        return None


class _Arbiter:
    def __init__(self, outcome, before_outcome=None) -> None:
        self._outcome, self._before = outcome, before_outcome

    def submit(self, action, *, on_outcome=None):
        if self._outcome is not None and on_outcome is not None:
            on_outcome(self._outcome)
        if self._before is not None:
            self._before()
        return Rejection.OK


def _dispatch(frames: _Frames, outcome):
    lens = object.__new__(GameLens)
    lens.log = ActionLog()
    lens.capture = type("C", (), {"frames": frames})()
    # Frames keep arriving after the outcome; the report must not follow them.
    lens.arbiter = _Arbiter(outcome, before_outcome=lambda: setattr(frames, "latest", 999))
    action = type("A", (), {"action_id": 3, "steps": []})()
    return GameLens._dispatch(lens, action, None, None, "t", wait=0.5)


def test_after_frame_is_the_id_current_when_the_outcome_arrived():
    result = _dispatch(_Frames(41), Outcome("sent", "", 3))
    assert result.after_frame == 41
    assert result.to_dict()["after_frame"] == 41


@pytest.mark.parametrize("status", ["denied", "dry-run"])
def test_nothing_injected_means_no_after_frame(status):
    assert _dispatch(_Frames(41), Outcome(status, "", 3)).after_frame is None


def test_no_outcome_yet_means_no_after_frame():
    assert _dispatch(_Frames(41), None).after_frame is None


def test_before_any_frame_there_is_nothing_to_wait_past():
    assert _dispatch(_Frames(0), Outcome("sent", "", 3)).after_frame is None


# --- HTTP ------------------------------------------------------------------


class FrameRuntime(FakeRuntime):
    """A runtime whose frames are scripted: an id counter and encode results."""

    def __init__(self) -> None:
        super().__init__()
        self.latest = 0
        self.script: list = []          # results encode_frame returns, in order
        self.encodes = 0

    def latest_frame_id(self) -> int:
        return self.latest

    def encode_frame(self, quality: int = 70, *, min_frame_id=None, retain=True):
        self.encodes += 1
        if self.script:
            return self.script.pop(0)
        if self.latest == 0 or (min_frame_id is not None and self.latest < min_frame_id):
            return NO_FRAME
        return Encoded(b"jpeg%d" % self.latest, "obs-%d" % self.latest, self.latest)


@pytest.fixture
def frt() -> FrameRuntime:
    return FrameRuntime()


@pytest.fixture
def fclient(frt):
    # One portal for every request: the worker pool a sync route is served
    # from is per event loop, and the /stop test is about sharing it.
    with TestClient(create_app(frt), base_url="http://127.0.0.1:8777") as c:
        yield c


def test_frame_names_the_frame_it_is(fclient, frt):
    frt.latest = 17
    r = fclient.get("/frame.jpg", headers=agent(frt))
    assert r.status_code == 200
    assert r.headers["x-gamelens-frame"] == "17"
    assert r.headers["x-gamelens-observation"] == "obs-17"


def test_no_frame_yet_is_503(fclient, frt):
    assert fclient.get("/frame.jpg", headers=agent(frt)).status_code == 503


def test_after_waits_for_a_frame_past_the_one_named(fclient, frt):
    frt.latest = 10
    threading.Timer(0.1, lambda: setattr(frt, "latest", 12)).start()
    started = time.monotonic()
    r = fclient.get("/frame.jpg?after=10&frames=2&wait_ms=1000", headers=agent(frt))
    assert r.status_code == 200
    assert int(r.headers["x-gamelens-frame"]) >= 12
    assert time.monotonic() - started >= 0.09


def test_after_that_never_arrives_is_504_not_the_stale_frame(fclient, frt):
    frt.latest = 10
    r = fclient.get("/frame.jpg?after=10&frames=1&wait_ms=100", headers=agent(frt))
    assert r.status_code == 504
    assert r.headers["x-gamelens-latest-frame"] == "10"
    assert "11" in r.json()["detail"]


def test_a_frame_cleared_between_look_and_lease_is_waited_through(fclient, frt):
    """Review R5: failover can clear the slot after the id qualified."""
    frt.latest = 20
    frt.script = [NO_FRAME, Encoded(b"x", "obs-late", 21)]
    r = fclient.get("/frame.jpg?after=19&wait_ms=1000", headers=agent(frt))
    assert r.status_code == 200
    assert r.headers["x-gamelens-frame"] == "21"
    assert frt.encodes == 2


def test_a_frame_that_stays_gone_times_out_as_504_not_503(fclient, frt):
    frt.latest = 20
    frt.script = [NO_FRAME] * 10_000
    r = fclient.get("/frame.jpg?after=19&wait_ms=150", headers=agent(frt))
    assert r.status_code == 504


def test_an_encode_failure_is_not_waited_on(fclient, frt):
    frt.latest = 20
    frt.script = [ENCODE_FAILED]
    started = time.monotonic()
    r = fclient.get("/frame.jpg?after=19&wait_ms=1000", headers=agent(frt))
    assert r.status_code == 503
    assert time.monotonic() - started < 0.5


@pytest.mark.parametrize("query", [
    "after=-1", "after=1&frames=0", "after=1&frames=31",
    "after=1&wait_ms=-1", "after=1&wait_ms=2001",
])
def test_out_of_range_parameters_are_refused_not_clamped(fclient, frt, query):
    frt.latest = 5
    assert fclient.get(f"/frame.jpg?{query}", headers=agent(frt)).status_code == 422


def test_frame_still_needs_a_token(fclient, frt):
    frt.latest = 5
    assert fclient.get("/frame.jpg?after=1").status_code == 401


def test_act_reports_after_frame(fclient, frt):
    frt.next_dispatch = Dispatch("ok", "sent", "", 1, after_frame=33)
    r = fclient.post("/act", headers=agent(frt),
                     json={"observation_id": "o", "kind": "click", "x": 1, "y": 1})
    assert r.status_code == 200
    assert r.json()["after_frame"] == 33


def test_stop_stays_prompt_while_frame_waits_are_saturated(fclient, frt):
    """Review R3: /stop is served from a worker thread, and a wait must not hold one."""
    frt.latest = 1
    url = "/frame.jpg?after=1000&wait_ms=2000"
    with ThreadPoolExecutor(max_workers=MAX_FRAME_WAITS + 20) as pool:
        waits = [pool.submit(fclient.get, url, headers=agent(frt))
                 for _ in range(MAX_FRAME_WAITS)]
        time.sleep(0.2)                       # let those four take the slots
        extra = [pool.submit(fclient.get, url, headers=agent(frt)) for _ in range(20)]
        refused = [f.result(timeout=5).status_code for f in extra]
        started = time.monotonic()
        stop = fclient.post("/stop", headers=op(frt))
        elapsed = time.monotonic() - started
        assert stop.status_code == 200 and frt.safety.killed
        assert elapsed < 0.5, f"/stop took {elapsed:.2f}s behind frame waits"
        assert refused == [429] * 20
        assert [f.result(timeout=5).status_code for f in waits] == [504] * MAX_FRAME_WAITS


def test_the_admission_slot_is_given_back(fclient, frt):
    frt.latest = 1
    for _ in range(MAX_FRAME_WAITS + 2):
        r = fclient.get("/frame.jpg?after=1000&wait_ms=0", headers=agent(frt))
        assert r.status_code == 504


def test_a_stream_part_names_the_frame_that_was_encoded(frt):
    """Review R4: the slot may already hold a newer frame than the one encoded."""
    frt.latest = 50                             # newer than what gets encoded
    frt.script = [Encoded(b"JPEG", "obs-7", 7)]
    app = create_app(frt)
    route = next(r for r in app.routes if getattr(r, "path", "") == "/stream.mjpg")
    import asyncio

    async def first_part():
        response = await route.endpoint(fps=60, quality=65, role="agent")
        async for chunk in response.body_iterator:
            return chunk

    part = asyncio.run(first_part())
    assert b"X-GameLens-Frame: 7\r\n" in part
    assert b"X-GameLens-Observation: obs-7\r\n" in part


# --- GameLens.encode_frame over the real slot ---------------------------------


class _Registry:
    def issue(self, observation, *, jpeg=None, quality=None, moving=None, retain=True) -> str:
        return "obs"


def _encoder(slot: LatestFrame, *, fail: bool = False) -> GameLens:
    lens = object.__new__(GameLens)
    lens.capture = type("C", (), {"frames": slot,
                                  "backend": type("B", (), {"session_id": 1})()})()

    def observation_for(frame, scale):
        if fail:
            raise RuntimeError("boom")
        return object()

    lens.arbiter = type("A", (), {"observation_for": staticmethod(observation_for)})()
    lens.observations = _Registry()
    lens.activity = ActivityMap()
    return lens


def test_encode_frame_refuses_a_frame_older_than_asked_for():
    pool, slot = FramePool(), LatestFrame()
    slot.publish(_frame(pool, 5))
    lens = _encoder(slot)
    assert GameLens.encode_frame(lens, min_frame_id=6) is NO_FRAME
    got = GameLens.encode_frame(lens, min_frame_id=5)
    assert isinstance(got, Encoded) and got.frame_id == 5
    assert GameLens.latest_frame_id(lens) == 5


def test_encode_frame_on_an_empty_slot_is_no_frame_not_a_failure():
    assert GameLens.encode_frame(_encoder(LatestFrame())) is NO_FRAME


def test_an_encode_that_raises_is_reported_as_such_and_the_lease_returned():
    pool, slot = FramePool(), LatestFrame()
    slot.publish(_frame(pool, 5))
    assert GameLens.encode_frame(_encoder(slot, fail=True)) is ENCODE_FAILED
    assert pool.stats()["double_release"] == 0
    slot.clear()
    assert pool.stats()["live"] == pool.stats()["free"]      # every buffer back
