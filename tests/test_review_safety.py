"""Review regressions, with no game capture or native input.

These tests use fakes for shutdown and injection, and temporary HTTP stubs for
transport. They do not Arm/Go live a real GameLens session.
"""
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock
import queue

import pytest

from gamelens.app import GameLens, Dispatch
from gamelens.input import InputExecutor, Sequence, MoveTo, Dwell
from gamelens.safety import NotPermitted, Denial


def test_shutdown_kills_before_waiting_for_recorder():
    events = []
    lens = SimpleNamespace(
        safety=SimpleNamespace(kill=lambda reason: events.append("kill"),
                               shutdown=lambda: events.append("safety")),
        recorder=SimpleNamespace(stop=lambda: events.append("recorder")),
        _stop=SimpleNamespace(set=lambda: events.append("stop")),
        _poller=None, agent=None,
        executor=SimpleNamespace(shutdown=lambda: events.append("executor")),
        capture=SimpleNamespace(stop=lambda: events.append("capture")),
    )
    GameLens.stop(lens)
    assert events[0:2] == ["kill", "recorder"]
    assert events.index("kill") < events.index("executor")


def fake_safety():
    return SimpleNamespace(on_kill=Mock(), commit=lambda: nullcontext(),
                           release_boundary=lambda: nullcontext(), dry_run=False)


def test_mid_sequence_denial_reports_partial_input(monkeypatch):
    import gamelens.input as module
    monkeypatch.setattr(module, "require_trustworthy_dpi", lambda: None)
    executor = InputExecutor(fake_safety())
    sent, outcomes = [], []
    executor._guard_input = lambda step: None
    executor._apply_new = lambda step: sent.append(step)
    checks = []

    def validate():
        checks.append(True)
        if len(checks) == 2:
            raise NotPermitted(Denial.NOT_FOREGROUND)

    sequence = Sequence([MoveTo(10, 10), MoveTo(20, 20)],
                        validate=validate, on_outcome=outcomes.append)
    executor.submit(sequence)
    executor._queue.put_nowait(None)
    executor._run()
    assert len(sent) == 1 and len(outcomes) == 1
    outcome = outcomes[0]
    assert outcome.status == "denied" and outcome.partial
    assert outcome.injected_steps == 1 and outcome.completed_steps == 1
    assert outcome.last_completed_step == 0 and outcome.reached_the_target
    assert "do not replay" in outcome.detail
    wire = Dispatch("ok", outcome.status, outcome.detail,
                    completed_steps=outcome.completed_steps,
                    injected_steps=outcome.injected_steps,
                    last_completed_step=outcome.last_completed_step,
                    partial=outcome.partial).to_dict()
    assert wire["partial"] and wire["injected_steps"] == 1


def test_denial_before_any_input_is_not_partial(monkeypatch):
    import gamelens.input as module
    monkeypatch.setattr(module, "require_trustworthy_dpi", lambda: None)
    executor = InputExecutor(fake_safety())
    results = []

    def deny():
        raise NotPermitted(Denial.NOT_FOREGROUND)

    executor.submit(Sequence([MoveTo(1, 1)], validate=deny, on_outcome=results.append))
    executor._queue.put_nowait(None)
    executor._run()
    assert not results[0].partial
    assert results[0].injected_steps == results[0].completed_steps == 0


def test_full_queue_refuses_without_waiting_and_reports_once():
    executor = InputExecutor(fake_safety(), queue_capacity=1)
    results = []
    assert executor.submit(Sequence([Dwell(.01)]))
    assert executor.submit(Sequence([], on_outcome=results.append)) is False
    assert executor._queue.qsize() == 1
    assert len(results) == 1 and results[0].status == "cancelled"
    assert "queue is full" in results[0].detail
    assert not results[0].partial
    executor.shutdown()
    assert executor.submit(Sequence([], on_outcome=results.append)) is False
    assert len(results) == 2


@pytest.mark.parametrize("capacity", [0, -1, True, 1.5])
def test_queue_capacity_is_validated(capacity):
    with pytest.raises(ValueError):
        InputExecutor(fake_safety(), queue_capacity=capacity)


def test_concurrent_shutdown_with_capacity_one_never_enqueues_a_sentinel():
    import threading
    executor = InputExecutor(fake_safety(), queue_capacity=1)
    assert executor.submit(Sequence([]))
    joined = threading.Barrier(2)
    executor._thread = SimpleNamespace(join=lambda timeout: joined.wait(timeout=2))
    errors = []

    def shutdown():
        try:
            executor.shutdown()
        except Exception as exc:
            errors.append(exc)

    callers = [threading.Thread(target=shutdown) for _ in range(2)]
    for thread in callers:
        thread.start()
    for thread in callers:
        thread.join(timeout=3)
    assert not any(thread.is_alive() for thread in callers)
    assert errors == []
    assert executor._queue.empty()


def test_kill_stops_idle_worker_without_shutdown_sentinel(monkeypatch):
    import threading
    import gamelens.input as module
    monkeypatch.setattr(module, "require_trustworthy_dpi", lambda: None)
    executor = InputExecutor(fake_safety(), queue_capacity=1)
    entered = threading.Event()
    original_get = executor._queue.get

    def observed_get(*args, **kwargs):
        entered.set()
        return original_get(*args, **kwargs)

    monkeypatch.setattr(executor._queue, "get", observed_get)
    thread = threading.Thread(target=executor._run, daemon=True)
    executor._thread = thread
    thread.start()
    try:
        assert entered.wait(timeout=2)
        executor._on_kill("test")
        thread.join(timeout=1)
        assert not thread.is_alive()
    finally:
        executor.shutdown()
