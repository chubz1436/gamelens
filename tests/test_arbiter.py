"""Arbiter tests: the cases where an action looks fine and is not.

The easy rejections (wrong window, obviously expired) are not where the value
is. These concentrate on the ones that pass every naive check: a stalled capture
behind an unmoved window, and a model finishing its thought after the world
moved on.
"""

from __future__ import annotations

import time

import pytest

from gamelens.arbiter import Arbiter, Rejection


class FakeBackend:
    def __init__(self, session_id: int = 1) -> None:
        self.session_id = session_id


class FakeBinding:
    def __init__(self, hwnd: int = 1234) -> None:
        self.hwnd = hwnd


class FakeFrame:
    def __init__(self, frame_id: int, session_id: int, captured_at: float) -> None:
        self.frame_id = frame_id
        self.session_id = session_id
        self.captured_at = captured_at
        self.width = 1920
        self.height = 1080
        self.released = False

    def release(self) -> None:
        self.released = True


class FakeFrames:
    def __init__(self) -> None:
        self.frame: FakeFrame | None = None

    def acquire(self, timeout=None):
        return self.frame


class FakeCapture:
    def __init__(self) -> None:
        self.binding = FakeBinding()
        self.frames = FakeFrames()
        self.backend = FakeBackend()


class FakeGeometry:
    def __init__(self, generation: int = 1) -> None:
        self.generation = generation

    @property
    def current(self):
        return self


class FakeTracker:
    def __init__(self) -> None:
        self._geom = FakeGeometry()

    @property
    def current(self):
        return self._geom

    @property
    def generation(self) -> int:
        return self._geom.generation

    def move(self) -> None:
        self._geom = FakeGeometry(self._geom.generation + 1)


class FakeExecutor:
    def __init__(self) -> None:
        self.submitted: list = []

    def submit(self, seq) -> None:
        self.submitted.append(seq)


@pytest.fixture
def parts():
    capture = FakeCapture()
    tracker = FakeTracker()
    executor = FakeExecutor()
    arbiter = Arbiter(capture, tracker, executor)
    capture.frames.frame = FakeFrame(1, 1, time.monotonic())
    return capture, tracker, executor, arbiter


def make_action(arbiter, observation, steps=None):
    from gamelens.arbiter import Action
    return Action(observation=observation, steps=steps or [], label="test")


def test_fresh_action_is_accepted(parts):
    capture, tracker, executor, arbiter = parts
    frame, obs = arbiter.observe()
    assert arbiter.submit(make_action(arbiter, obs)) is Rejection.OK
    assert executor.submitted


def test_wrong_target_is_rejected(parts):
    capture, tracker, executor, arbiter = parts
    _, obs = arbiter.observe()
    capture.binding.hwnd = 9999
    assert arbiter.evaluate(make_action(arbiter, obs)) is Rejection.WRONG_TARGET


def test_retired_backend_session_is_rejected(parts):
    """A backend swap invalidates frame ids and geometry from the old session."""
    capture, tracker, executor, arbiter = parts
    _, obs = arbiter.observe()
    capture.backend = FakeBackend(session_id=2)
    assert arbiter.evaluate(make_action(arbiter, obs)) is Rejection.RETIRED_SESSION


def test_geometry_change_is_rejected(parts):
    capture, tracker, executor, arbiter = parts
    _, obs = arbiter.observe()
    tracker.move()
    assert arbiter.evaluate(make_action(arbiter, obs)) is Rejection.GEOMETRY_MOVED


# --- GL-004: the cases naive checks miss -----------------------------------


def test_stalled_capture_unchanged_geometry(parts):
    """The headline case.

    The window has not moved, the backend is the same, and the action was
    created a moment ago -- so target, session, geometry and TTL all pass. What
    is wrong is that the *frame* behind it is seconds old because capture
    stalled. Without a source-frame age check this action sails through while
    describing a scene that no longer exists.
    """
    capture, tracker, executor, arbiter = parts
    capture.frames.frame = FakeFrame(1, 1, time.monotonic() - 5.0)
    _, obs = arbiter.observe()

    action = make_action(arbiter, obs)          # created right now
    assert action.age() < 0.1
    assert tracker.generation == obs.geometry_generation
    assert capture.backend.session_id == obs.backend_session_id

    assert arbiter.evaluate(action) is Rejection.STALE_OBSERVATION


def test_inference_completes_after_preemption(parts):
    """A reflex acted while the model was thinking, so its answer is void."""
    capture, tracker, executor, arbiter = parts
    _, obs = arbiter.observe()

    arbiter.preempt("reflex fired mid-inference")

    assert arbiter.evaluate(make_action(arbiter, obs)) is Rejection.PREEMPTED
    assert not executor.submitted


def test_expired_action_is_rejected(parts):
    capture, tracker, executor, arbiter = parts
    _, obs = arbiter.observe()
    action = make_action(arbiter, obs)
    action.created_at = time.monotonic() - 10.0
    assert arbiter.evaluate(action) is Rejection.EXPIRED


def test_no_frame_raises(parts):
    from gamelens.arbiter import ActionRejected
    capture, tracker, executor, arbiter = parts
    capture.frames.frame = None
    with pytest.raises(ActionRejected) as exc:
        arbiter.observe()
    assert exc.value.reason is Rejection.NO_FRAME


def test_rejections_are_counted(parts):
    capture, tracker, executor, arbiter = parts
    _, obs = arbiter.observe()
    tracker.move()
    arbiter.submit(make_action(arbiter, obs))
    assert arbiter.stats()["rejections"]["GEOMETRY_MOVED"] == 1
    assert arbiter.stats()["accepted"] == 0


# --- transform round trip ---------------------------------------------------


def test_downscaled_coordinates_map_back(parts):
    """The model sees a smaller picture; its coordinates must scale back up."""
    capture, tracker, executor, arbiter = parts
    _, obs = arbiter.observe(scale=0.5)
    assert obs.to_frame_xy(100, 50) == (200.0, 100.0)


def test_crop_offset_is_applied(parts):
    capture, tracker, executor, arbiter = parts
    _, obs = arbiter.observe(scale=1.0, crop=(40, 60))
    assert obs.to_frame_xy(10, 10) == (50.0, 70.0)


# --- lease ownership -------------------------------------------------------


def test_observe_returns_a_lease_the_caller_must_release(parts):
    """observe() hands over ownership; nothing releases it on the caller's behalf."""
    capture, tracker, executor, arbiter = parts
    frame, _ = arbiter.observe()
    assert frame is not None
    assert not frame.released
    frame.release()
    assert frame.released


def test_observation_for_takes_no_lease(parts):
    """A caller already holding a frame must not be made to acquire a second one.

    The regression this guards: app.submit_click acquired a frame, checked its
    id, then called observe() -- which took another lease that was discarded and
    never released. Four of those and the buffer pool was empty, after which
    capture silently dropped every frame.
    """
    capture, tracker, executor, arbiter = parts

    acquisitions = {"count": 0}
    real_acquire = capture.frames.acquire

    def counting_acquire(timeout=None):
        acquisitions["count"] += 1
        return real_acquire(timeout)

    capture.frames.acquire = counting_acquire

    held = capture.frames.acquire()
    acquisitions["count"] = 0

    observation = arbiter.observation_for(held)

    assert acquisitions["count"] == 0, "observation_for must not acquire a frame"
    assert observation.frame_id == held.frame_id
    assert observation.backend_session_id == held.session_id


def test_observation_for_matches_the_frame_it_was_given(parts):
    """Provenance must describe the caller's frame, not whatever is newest."""
    capture, tracker, executor, arbiter = parts
    held = capture.frames.acquire()
    capture.frames.frame = FakeFrame(99, 1, time.monotonic())   # newer arrives

    observation = arbiter.observation_for(held)
    assert observation.frame_id == held.frame_id != 99
