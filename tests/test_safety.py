"""Interlock tests. Every one of these asserts a *denial* path, because the
failure mode that matters is the guard saying yes when it should have said no."""

from __future__ import annotations

import threading
import time

import pytest
import win32gui

from gamelens.safety import Denial, NotPermitted, SafetySupervisor, TokenBucket


@pytest.fixture
def hwnd() -> int:
    return win32gui.GetForegroundWindow()


@pytest.fixture
def sup(hwnd: int):
    s = SafetySupervisor(hwnd, rate=1000.0)
    s.start()
    yield s
    s.shutdown()


def test_disarmed_by_default(sup):
    assert sup.check() is Denial.NOT_ARMED
    assert not sup.armed


def test_dry_run_is_the_default(sup):
    sup.arm()
    assert sup.dry_run is True, "arming must not also go live"


def test_go_live_requires_arming_first(sup):
    with pytest.raises(NotPermitted) as exc:
        sup.go_live()
    assert exc.value.reason is Denial.NOT_ARMED


def test_kill_latches_and_cannot_be_rearmed(sup):
    sup.arm()
    sup.kill("test")
    assert sup.check() is Denial.KILLED
    with pytest.raises(NotPermitted):
        sup.arm()
    assert sup.check() is Denial.KILLED, "kill must survive a re-arm attempt"


def test_rate_limit_denies_after_burst(hwnd):
    s = SafetySupervisor(hwnd, rate=3.0)
    s.start()
    try:
        s.arm()
        verdicts = [s.check() for _ in range(5)]
        assert Denial.RATE_LIMITED in verdicts
    finally:
        s.shutdown()


def test_foreground_guard(sup, monkeypatch):
    sup.arm()
    assert sup.check() is Denial.OK
    monkeypatch.setattr("gamelens.safety.win32gui.GetForegroundWindow", lambda: 0xDEAD)
    assert sup.check() is Denial.NOT_FOREGROUND


def test_guard_denies_when_a_check_raises(sup, monkeypatch):
    sup.arm()

    def boom():
        raise OSError("simulated Win32 failure")

    monkeypatch.setattr("gamelens.safety.win32gui.GetForegroundWindow", boom)
    assert sup.check() is Denial.GUARD_ERROR, "an exception inside the guard must deny"


# --- GL-006: liveness, not just freshness ---------------------------------


def test_watchdog_death_immediately_after_heartbeat(sup):
    """A dead thread must deny the *next* action, not 500ms of them.

    The heartbeat is deliberately left fresh here. Freshness alone would admit
    actions for the whole HEARTBEAT_MAX_AGE window after the thread exited, so
    this asserts the liveness half of the check in isolation.
    """
    sup.arm()
    assert sup.check() is Denial.OK

    dead = threading.Thread(target=lambda: None)
    dead.start()
    dead.join()
    assert not dead.is_alive()

    sup._heartbeat = time.monotonic()      # fresh on purpose
    sup._thread = dead                     # but not running

    assert sup.check() is Denial.WATCHDOG_DEAD


def test_stale_heartbeat_denies(sup):
    sup.arm()
    sup._heartbeat = time.monotonic() - 10.0
    assert sup.check() is Denial.WATCHDOG_STALE


def test_heartbeat_starts_invalid(hwnd):
    s = SafetySupervisor(hwnd)
    s._armed = True                        # bypass arm(); watchdog never started
    assert s._heartbeat is None
    assert s.check() in (Denial.WATCHDOG_DEAD, Denial.WATCHDOG_STALE)


# --- GL-014: no gap between authorization and the press --------------------


def test_commit_and_kill_are_totally_ordered(sup):
    """A kill must never complete *between* an approval and the press it approved.

    The old guard read the kill latch, released its lock, then polled the
    foreground window and the rate limiter. A kill landing in that gap finished
    its cleanup callbacks and the guard still returned OK -- so a press followed
    cleanup and nothing was left to release it.

    Here the check is paused mid-flight (inside the foreground poll) while
    another thread calls kill(). The assertion is that kill cannot complete until
    the committed body has run.
    """
    sup.arm()

    inside_check = threading.Event()
    let_check_finish = threading.Event()
    order: list[str] = []

    real_fg = win32gui.GetForegroundWindow

    def slow_foreground():
        inside_check.set()
        let_check_finish.wait(2.0)
        return real_fg()

    sup.on_kill(lambda reason: order.append("cleanup"))

    import gamelens.safety as safety_mod
    original = safety_mod.win32gui.GetForegroundWindow
    safety_mod.win32gui.GetForegroundWindow = slow_foreground
    try:
        def do_press():
            with sup.commit():
                order.append("press")

        presser = threading.Thread(target=do_press)
        presser.start()
        assert inside_check.wait(2.0), "check never started"

        killer = threading.Thread(target=lambda: sup.kill("race"))
        killer.start()
        time.sleep(0.05)

        # The kill is blocked on the dispatch boundary; cleanup has not run.
        assert "cleanup" not in order, "kill completed while a press was committing"

        let_check_finish.set()
        presser.join(2.0)
        killer.join(2.0)
    finally:
        safety_mod.win32gui.GetForegroundWindow = original

    assert order == ["press", "cleanup"], (
        f"press must land before cleanup enumerates what to release, got {order}"
    )


def test_commit_denies_once_killed(sup):
    sup.arm()
    sup.kill("test")
    with pytest.raises(NotPermitted) as exc:
        with sup.commit():
            pytest.fail("commit body must not run after a kill")
    assert exc.value.reason is Denial.KILLED


def test_commit_does_not_double_charge_the_rate_limiter(hwnd):
    s = SafetySupervisor(hwnd, rate=5.0)
    s.start()
    try:
        s.arm()
        before = s._bucket.peek()
        with s.commit():
            pass
        after = s._bucket.peek()
        assert before - after == pytest.approx(1.0, abs=0.2)
    finally:
        s.shutdown()


# --- token bucket ----------------------------------------------------------


def test_token_bucket_refills():
    b = TokenBucket(rate=100.0, capacity=2.0)
    assert b.take() and b.take()
    assert not b.take()
    time.sleep(0.05)
    assert b.take(), "bucket should refill over time"
