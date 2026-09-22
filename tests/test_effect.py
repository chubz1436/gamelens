"""GL-036: an action can be injected perfectly and accomplish nothing.

A reflex loop ran a hundred and sixty actions against the game and changed
nothing in it. Every one returned ``sent`` and every one was true: the mining
hold was 0.55s and the block needed 0.75s, so nothing ever broke. Acceptance was
right, execution was right, and the only thing that disagreed was the screen --
which the harness never looked at.

These tests are about the signal that closes that gap, and about the two ways it
could be quietly useless: measured against a buffer the pool has already reused,
or reported as a verdict it is not entitled to give.
"""

from __future__ import annotations

import numpy as np
import pytest

from gamelens.app import CHURN_FLOOR, CHURN_SIZE, Dispatch, GameLens
from gamelens.arbiter import Rejection
from gamelens.input import Outcome
from gamelens.server import ActionLog


class _Frame:
    """A lease over a pooled buffer, released back on exit like the real one."""

    def __init__(self, pool: "_Pool") -> None:
        self._pool = pool

    @property
    def array(self) -> np.ndarray:
        return self._pool.buffer

    def release(self) -> None:
        self._pool.released += 1


class _Pool:
    """One reused buffer, which is what makes the copy in _thumbnail load-bearing.

    The real pool hands out a lease over a buffer it will overwrite as soon as
    the lease is dropped. A thumbnail that kept a view rather than a copy would
    therefore end up comparing the after-frame against itself.
    """

    def __init__(self, height: int = 72, width: int = 128) -> None:
        self.buffer = np.zeros((height, width, 4), dtype=np.uint8)
        self.released = 0
        self.empty = False

    def acquire(self, timeout=None):
        return None if self.empty else _Frame(self)

    def fill(self, value: int) -> None:
        self.buffer[:, :, :3] = value


class _Arbiter:
    def __init__(self, verdict, outcome, on_submit=None) -> None:
        self._verdict, self._outcome, self._on_submit = verdict, outcome, on_submit

    def submit(self, action, *, on_outcome=None):
        if self._on_submit is not None:
            self._on_submit()
        if self._outcome is not None and on_outcome is not None:
            on_outcome(self._outcome)
        return self._verdict


def _lens(pool: _Pool, arbiter) -> GameLens:
    """A GameLens stand-in carrying only what _dispatch touches."""
    lens = object.__new__(GameLens)
    lens.log = ActionLog()
    lens.arbiter = arbiter
    lens.capture = type("C", (), {"frames": pool})()
    return lens


def _action():
    return type("A", (), {"action_id": 7, "steps": []})()


def _dispatch(lens, *, wait=0.5, measure=True) -> Dispatch:
    return GameLens._dispatch(lens, _action(), None, None, "probe",
                              wait=wait, measure=measure)


# --- the measurement itself ------------------------------------------------


def test_a_still_screen_reports_no_change():
    """The exact case that went undetected: sent, and nothing happened."""
    pool = _Pool()
    pool.fill(40)
    lens = _lens(pool, _Arbiter(Rejection.OK, Outcome("sent", "", 7)))
    result = _dispatch(lens)
    assert result.outcome == "sent"
    assert result.churn == 0.0
    assert result.changed_anything is False


def test_a_screen_that_moved_reports_change():
    pool = _Pool()
    pool.fill(10)
    lens = _lens(pool, _Arbiter(Rejection.OK, Outcome("sent", "", 7),
                                on_submit=lambda: pool.fill(200)))
    result = _dispatch(lens)
    assert result.churn is not None and result.churn > CHURN_FLOOR
    assert result.changed_anything is True


def test_the_thumbnail_is_a_copy_not_a_view_of_the_pool_buffer():
    """The pool reuses its buffer, so a view would compare a frame to itself.

    This is the failure that would leave GL-036 open while looking closed: the
    number would be reported, it would always be zero, and it would be zero for
    exactly the reason the feature exists to detect.
    """
    pool = _Pool()
    pool.fill(10)
    before = GameLens._thumbnail(_lens(pool, None))
    pool.fill(210)                       # the pool overwrites in place
    after = GameLens._thumbnail(_lens(pool, None))
    assert not np.array_equal(before, after), "before must not track the buffer"
    assert float(np.abs(after - before).mean()) > CHURN_FLOOR


def test_the_thumbnail_leaves_no_lease_outstanding():
    """A leaked lease starves the pool, which shows up as dropped frames later."""
    pool = _Pool()
    GameLens._thumbnail(_lens(pool, None))
    assert pool.released == 1


def test_the_thumbnail_is_signed_so_differences_do_not_wrap():
    """uint8 subtraction wraps: 10 - 210 is 56, not 200.

    On unsigned pixels a large darkening reads as a small change, so the signal
    would be quietest exactly when the screen moved most.
    """
    pool = _Pool()
    pool.fill(10)
    thumb = GameLens._thumbnail(_lens(pool, None))
    assert thumb.dtype == np.int16
    assert thumb.shape == (CHURN_SIZE[1], CHURN_SIZE[0])


# --- what it must not claim ------------------------------------------------


def test_churn_is_not_measured_unless_the_caller_asks():
    """It costs a settle delay, so it is off unless the request wants it.

    Roughly 150ms on top of an action that otherwise takes 23ms. A loop running
    at forty actions a second would lose most of its rate to a diagnostic it was
    not using.
    """
    pool = _Pool()
    lens = _lens(pool, _Arbiter(Rejection.OK, Outcome("sent", "", 7)))
    assert _dispatch(lens, measure=False).churn is None


def test_an_action_that_was_never_injected_is_not_measured():
    """Found live: every denied action reported a confident 0.00.

    A refused action settles in microseconds, so before and after came from the
    same captured frame and the answer was always "nothing changed" -- which
    reads as a working measurement and is in fact a frame compared with itself.
    Nothing was injected, so the screen is not evidence about it either way.
    """
    pool = _Pool()
    pool.fill(10)
    lens = _lens(pool, _Arbiter(Rejection.OK, Outcome("denied", "not armed", 7),
                                on_submit=lambda: pool.fill(200)))
    result = _dispatch(lens)
    assert result.outcome == "denied"
    assert result.churn is None, "a refused action has no effect to measure"


def test_the_measurement_straddles_the_action_rather_than_spanning_it():
    """The screen is sampled after the executor finishes, not while it runs.

    A mining hold animates cracks on the block for as long as the button is
    down, so a comparison taken across the action reports motion whether or not
    anything broke -- which is the exact defect this was written to catch. The
    "after" therefore has to come from the settled screen.
    """
    pool = _Pool()
    pool.fill(10)
    seen = []

    class _Recording(_Arbiter):
        def submit(self, action, *, on_outcome=None):
            pool.fill(250)                      # a transient during the action
            verdict = super().submit(action, on_outcome=on_outcome)
            pool.fill(10)                       # ... which then ends
            return verdict

    lens = _lens(pool, _Recording(Rejection.OK, Outcome("sent", "", 7)))
    result = _dispatch(lens)
    assert result.churn == 0.0, (
        "a transient that ends must not be reported as a change")


def test_no_frames_means_unmeasured_not_unchanged():
    """Absence of evidence is reported as absence, not as a zero."""
    pool = _Pool()
    pool.empty = True
    lens = _lens(pool, _Arbiter(Rejection.OK, Outcome("sent", "", 7)))
    result = _dispatch(lens)
    assert result.churn is None
    assert result.changed_anything is None


def test_churn_does_not_decide_whether_the_action_was_ok():
    """Evidence about the world is not a verdict about the action.

    Walking into a wall is a correct action with a still screen; rain falls in
    front of a refused one. Folding this into ``ok`` would make the harness
    wrong in both directions at once.
    """
    pool = _Pool()
    pool.fill(40)
    lens = _lens(pool, _Arbiter(Rejection.OK, Outcome("sent", "", 7)))
    result = _dispatch(lens)
    assert result.changed_anything is False
    assert result.ok is True


def test_a_rejected_action_is_never_credited_with_a_measurement():
    """Nothing was queued, so there is nothing for the screen to be evidence of."""
    pool = _Pool()
    lens = _lens(pool, _Arbiter(Rejection.GEOMETRY_MOVED, None))
    result = _dispatch(lens)
    assert result.verdict == "GEOMETRY_MOVED"
    assert result.churn is None


def test_the_wire_format_carries_it():
    assert "churn" in Dispatch("ok", "sent", "", 1, churn=4.5).to_dict()


def test_a_broken_capture_does_not_fail_the_action():
    """The measurement is bolted onto the path that presses buttons in a game.

    Adding it coupled dispatch to capture, and the first thing that coupling did
    was break three unrelated tests whose stand-in had no capture at all. In
    production the same shape is a dispatch that raises because the thing
    watching it failed -- an action refused for a reason that has nothing to do
    with the action.
    """
    class _Exploding:
        def acquire(self, timeout=None):
            raise RuntimeError("backend is mid-transition")

    lens = _lens(_Exploding(), _Arbiter(Rejection.OK, Outcome("sent", "", 7)))
    result = _dispatch(lens)
    assert result.outcome == "sent", "the action must survive its own instrumentation"
    assert result.churn is None
