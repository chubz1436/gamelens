"""GL-034 and GL-035.

Both defects had the same shape: every counter and every verdict inside the
process said the action succeeded, and the only thing that disagreed was the
target application's own screen. These tests assert the two facts that were
missing -- that a sequence always reports what really became of it, and that a
move always reaches the target as an event.
"""

from __future__ import annotations

import threading
import time

import pytest
import win32gui

from gamelens.input import (
    _MOVE_FLAGS,
    ButtonDown,
    ButtonUp,
    Dwell,
    InputExecutor,
    MoveTo,
    Outcome,
    Sequence,
    move_events,
)
from gamelens.safety import Denial, NotPermitted, SafetySupervisor


@pytest.fixture
def hwnd() -> int:
    return win32gui.GetForegroundWindow()


@pytest.fixture
def sup(hwnd: int):
    s = SafetySupervisor(hwnd, rate=1000.0)
    s.start()
    yield s
    s.shutdown()


@pytest.fixture
def executor(sup):
    ex = InputExecutor(sup, move_settle=0.001, press_hold=0.001)
    ex.start()
    yield ex
    ex.shutdown()


class Collector:
    """Records outcomes and lets a test wait for one."""

    def __init__(self) -> None:
        self.outcomes: list[Outcome] = []
        self.got = threading.Event()

    def __call__(self, outcome: Outcome) -> None:
        self.outcomes.append(outcome)
        self.got.set()

    def wait(self, timeout: float = 3.0) -> Outcome:
        assert self.got.wait(timeout), "no outcome was ever reported"
        return self.outcomes[0]


# --- GL-034: acceptance is not execution -----------------------------------


def test_a_dry_run_sequence_reports_dry_not_sent(executor, sup):
    """Dry-run authorizes every step and injects none of them.

    From the queue's point of view the two are identical, which is exactly why
    the executor has to say which one happened rather than letting the caller
    infer it from a flag it read at a different moment.
    """
    sup.arm()                      # armed but still dry
    seen = Collector()
    executor.submit(Sequence(
        steps=[MoveTo(10, 10), Dwell(0.001), ButtonDown(), ButtonUp()],
        label="test:dry", meta={"action_id": 41}, on_outcome=seen,
    ))
    outcome = seen.wait()
    assert outcome.status == "dry"
    assert outcome.action_id == 41
    assert not outcome.reached_the_target


def test_a_denied_sequence_reports_the_reason(executor, sup, monkeypatch):
    """Never armed, so the guard refuses -- and says which interlock refused."""
    seen = Collector()
    executor.submit(Sequence(
        steps=[MoveTo(10, 10)], label="test:denied",
        meta={"action_id": 42}, on_outcome=seen,
    ))
    outcome = seen.wait()
    assert outcome.status == "denied"
    assert outcome.detail == Denial.NOT_ARMED.value


def test_a_press_time_rejection_reports_denied(executor, sup):
    """The arbiter's revalidation runs inside the boundary and can still refuse.

    It raises its own exception type, which the executor must not collapse into
    a generic error -- the reason is the entire diagnostic value.
    """
    sup.arm()

    class Refused(RuntimeError):
        reason = Denial.NOT_FOREGROUND        # duck-typed like ActionRejected

    def validate() -> None:
        raise Refused("moved on")

    seen = Collector()
    executor.submit(Sequence(
        steps=[MoveTo(10, 10)], label="test:revalidate",
        meta={"action_id": 43}, validate=validate, on_outcome=seen,
    ))
    outcome = seen.wait()
    assert outcome.status == "denied"
    assert outcome.detail == Denial.NOT_FOREGROUND.value


def test_a_killed_queue_reports_cancelled_for_everything_in_it(sup):
    """A kill drains the queue. Every drained sequence still owes an answer.

    A caller blocked on an outcome that is never delivered waits out its whole
    timeout and then reports "pending" for something that was definitively
    cancelled.
    """
    ex = InputExecutor(sup, move_settle=0.001, press_hold=0.001)
    # Deliberately not started: nothing drains the queue until the kill does.
    sup.arm()
    collectors = [Collector() for _ in range(3)]
    for i, seen in enumerate(collectors):
        ex.submit(Sequence(
            steps=[MoveTo(10, 10)], label=f"test:q{i}",
            meta={"action_id": 50 + i}, on_outcome=seen,
        ))

    sup.kill("test")

    for seen in collectors:
        outcome = seen.wait(1.0)
        assert outcome.status == "cancelled"
    assert ex.cancelled == 3


def test_every_sequence_reports_exactly_once(executor, sup):
    sup.arm()
    seen = Collector()
    executor.submit(Sequence(
        steps=[MoveTo(10, 10), Dwell(0.001), ButtonDown(), ButtonUp()],
        label="test:once", meta={"action_id": 60}, on_outcome=seen,
    ))
    seen.wait()
    time.sleep(0.2)
    assert len(seen.outcomes) == 1


def test_a_live_sequence_reports_sent(executor, sup, monkeypatch):
    """The positive case, with the OS call stubbed.

    Asserting this for real would drag the operator's cursor across the screen
    from inside a test run, so SendInput is replaced -- the subject here is what
    the executor concludes, not whether Windows accepts the events.
    """
    sent: list = []
    monkeypatch.setattr("gamelens.input._send", lambda events: sent.append(events))
    sup.arm()
    sup.go_live()
    seen = Collector()
    executor.submit(Sequence(
        steps=[MoveTo(10, 10), Dwell(0.001), ButtonDown(), Dwell(0.001), ButtonUp()],
        label="test:live", meta={"action_id": 80}, on_outcome=seen,
    ))
    outcome = seen.wait()
    assert outcome.status == "sent"
    assert outcome.reached_the_target
    assert sent, "a live sequence must actually reach SendInput"


def test_a_raising_callback_does_not_kill_the_executor(executor, sup):
    """A bad consumer must not take the input thread down with it.

    If it did, every later action would sit in the queue unexecuted while the
    dashboard went on reporting a healthy executor.
    """
    sup.arm()

    def boom(outcome: Outcome) -> None:
        raise ValueError("consumer is broken")

    executor.submit(Sequence(
        steps=[MoveTo(10, 10)], label="test:boom",
        meta={"action_id": 70}, on_outcome=boom,
    ))
    seen = Collector()
    executor.submit(Sequence(
        steps=[MoveTo(10, 10)], label="test:after",
        meta={"action_id": 71}, on_outcome=seen,
    ))
    assert seen.wait().action_id == 71


# --- GL-035: a move the target never hears about ---------------------------


def test_move_to_a_fresh_point_is_one_event(monkeypatch):
    monkeypatch.setattr("gamelens.input.cursor_position", lambda: (0, 0))
    events = move_events(400, 400)
    assert len(events) == 1


def test_move_to_the_cursors_own_pixel_is_split_in_two(monkeypatch):
    """Windows emits nothing for a move to the pixel the cursor already holds.

    Measured against a Tk window: an absolute SendInput move to its own current
    cursor position produced no motion event at all, only the press that
    followed. An application that tracks the pointer through move events is then
    pressed at a coordinate it was never told about and ignores it -- while
    SendInput returns a full count and the executor records a clean execution.
    """
    monkeypatch.setattr("gamelens.input.cursor_position", lambda: (400, 400))
    events = move_events(400, 400)
    assert len(events) == 2, "the cursor must be stepped aside so a move is emitted"
    assert events[-1].mi.dwFlags == _MOVE_FLAGS
    assert (events[0].mi.dx, events[0].mi.dy) != (events[1].mi.dx, events[1].mi.dy)


def test_a_one_pixel_difference_also_counts_as_already_there(monkeypatch):
    """Normalization rounds, so a neighbouring pixel can map to the same pair.

    Treating only an exact match as "already there" would leave the suppressed
    case unfixed for every coordinate that happens to round together.
    """
    monkeypatch.setattr("gamelens.input.cursor_position", lambda: (401, 400))
    assert len(move_events(400, 400)) == 2


def test_an_unreadable_cursor_does_not_split_the_move(monkeypatch):
    """GetCursorPos can fail. Falling back to the plain move is the safe side:
    it is what the code always did, and it never invents an extra event."""
    monkeypatch.setattr("gamelens.input.cursor_position", lambda: None)
    assert len(move_events(400, 400)) == 1


# --- the log entry a dashboard actually reads ------------------------------


class _FakeArbiter:
    """Accepts, then reports whatever outcome the test wants, synchronously."""

    def __init__(self, verdict, outcome: Outcome | None) -> None:
        self._verdict = verdict
        self._outcome = outcome

    def submit(self, action, *, on_outcome=None):
        if self._outcome is not None and on_outcome is not None:
            on_outcome(self._outcome)
        return self._verdict


def _dispatch_with(verdict, outcome):
    """Drive GameLens._dispatch against a real ActionLog and a fake arbiter."""
    from gamelens.app import GameLens
    from gamelens.server import ActionLog

    stand_in = type("R", (), {})()
    stand_in.log = ActionLog()
    stand_in.arbiter = _FakeArbiter(verdict, outcome)
    # `steps` matters: the wait budget is read off the action's own dwells, so
    # a fake without them is not a stand-in for anything real.
    action = type("A", (), {"action_id": 99, "steps": []})()
    result = GameLens._dispatch(stand_in, action, 10, 20, "probe", wait=0.5)
    return result, stand_in.log


def test_the_log_entry_is_resolved_to_what_actually_happened():
    """The entry starts at "queued" and must end at the executor's word.

    The old code wrote "sent" the moment the arbiter accepted, so the dashboard
    showed a green line and a green overlay mark for a click that the executor
    went on to refuse. The interlocks were never the problem; the report was.
    """
    from gamelens.arbiter import Rejection

    result, log = _dispatch_with(
        Rejection.OK, Outcome("denied", "target window is not in the foreground", 99),
    )
    assert result.verdict == "ok"
    assert result.outcome == "denied"
    assert not result.ok

    entry = log.entries()[-1]
    assert entry["status"] == "denied"
    assert "foreground" in entry["text"]
    assert log.marks()[-1]["status"] == "denied", "the overlay mark must move too"


def test_an_action_the_arbiter_refuses_never_sits_at_queued():
    """Nothing was queued, so no outcome is coming; the entry must not hang."""
    from gamelens.arbiter import Rejection

    result, log = _dispatch_with(Rejection.GEOMETRY_MOVED, None)
    assert result.verdict == "GEOMETRY_MOVED"
    assert log.entries()[-1]["status"] == "denied"


def test_a_slow_executor_leaves_the_caller_with_pending_not_ok():
    from gamelens.arbiter import Rejection

    result, log = _dispatch_with(Rejection.OK, None)   # outcome never arrives
    assert result.outcome == "pending"
    assert log.entries()[-1]["status"] == "queued", "still genuinely in the queue"


def test_the_wait_budget_covers_the_action_s_own_length():
    """A two-second key hold must not report "pending" for working correctly.

    The budget is the queue allowance plus however long the action's own dwells
    will take. Without that, every long press answers a question nobody asked:
    truthfully, that no outcome arrived in 1.5s; uselessly, because it was never
    going to.
    """
    from gamelens.app import DISPATCH_WAIT, _expected_duration
    from gamelens.input import Dwell, KeyDown, KeyUp

    action = type("A", (), {
        "action_id": 1,
        "steps": [KeyDown(0x57), Dwell(2.0), KeyUp(0x57)],
    })()
    assert _expected_duration(action) == 2.0
    assert DISPATCH_WAIT + _expected_duration(action) > 2.0
