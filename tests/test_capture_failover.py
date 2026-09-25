"""GL-041: a target that goes dark for a while must not end capture for good.

Found on the Hyper-V test VM: Bedrock presents nothing while it generates a
world, so WGC, PrintWindow and mss each missed the 0.5 s deadline in turn, the
supervisor logged "failover exhausted" and returned, and /frame.jpg answered
503 for the rest of the process's life with the game running normally.

The backends here are fakes whose health the test switches, so the supervisor's
own thread is exercised exactly as it runs, without any window.
"""

from __future__ import annotations

import time
from types import SimpleNamespace

import numpy as np
import pytest

from gamelens import capture
from gamelens.capture import Backend, CaptureSupervisor


class World:
    """What every fake backend consults: may it start, is it producing."""

    def __init__(self):
        self.can_start = True
        self.producing = True
        self.started: list[str] = []
        self.blocked: set[str] = set()     # kinds that refuse to start
        self.dark: set[str] = set()        # kinds that start but never produce
        self.lives: dict[str, float] = {}  # kinds that produce only this long
        self.slow: dict[str, float] = {}   # kinds whose start takes this long
        self.hang = None                   # an Event every stop() waits on, if set
        self.instances: list = []


def fake_class(kind: Backend, world: World):
    class Fake(capture.CaptureBackend):
        def start(self):
            world.instances.append(self)
            time.sleep(world.slow.get(kind.value, 0))
            if not world.can_start or kind.value in world.blocked:
                raise OSError("nothing to capture")
            world.started.append(kind.value)
            self._publish(np.zeros((4, 4, 4), np.uint8), 0)

        def stop(self):
            self.retire()
            if world.hang is not None:
                world.hang.wait()

        def healthy(self, deadline=capture.FRAME_DEADLINE):
            life = world.lives.get(kind.value)
            return (not self.retired and not self.error and world.producing
                    and kind.value not in world.dark
                    and (life is None or time.monotonic() - self.activated_at < life))

    Fake.kind = kind
    return Fake


@pytest.fixture
def world(monkeypatch):
    w = World()
    monkeypatch.setattr(capture, "_BACKEND_CLASSES",
                        {k: fake_class(k, w) for k in Backend})
    monkeypatch.setattr(capture, "title_still_owned_by", lambda hwnd, title: True)
    monkeypatch.setattr(capture, "RETRY_BACKOFF", (0.1, 0.2))
    monkeypatch.setattr(capture, "PROMOTE_AFTER", 0.3)
    monkeypatch.setattr(capture, "PROMOTE_MAX", 1.2)
    return w


def target():
    return SimpleNamespace(hwnd=1, title="Game", width=4, height=4)


def wait_for(cond, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


def supervisor(**kw):
    sup = CaptureSupervisor(target(), **kw)
    sup.start()
    return sup


def test_capture_returns_after_every_backend_failed(world):
    sup = supervisor()
    try:
        assert sup.backend.kind is Backend.WGC
        # The game stops presenting and nothing new can start: every backend
        # in the order fails, which is where capture used to end.
        world.can_start = False
        world.producing = False
        assert wait_for(lambda: any("retrying" in t for t in sup.transitions))
        assert sup.backend is None
        # The game comes back.
        world.can_start = True
        world.producing = True
        assert wait_for(lambda: sup.backend is not None)
        # From the top of the order, not wherever the failover stopped.
        assert sup.backend.kind is Backend.WGC
    finally:
        sup.stop()


def test_a_fallback_steps_back_up_once_the_top_works_again(world):
    # The load that sent capture down to mss is over; staying on mss -- which
    # captures whatever covers the window -- for the rest of the process was
    # the second half of GL-041.
    sup = supervisor()
    try:
        world.dark = {"wgc", "printwindow"}
        assert wait_for(lambda: sup.backend is not None and sup.backend.kind is Backend.MSS)
        world.dark = set()
        assert wait_for(lambda: sup.backend is not None and sup.backend.kind is Backend.WGC)
        assert any("stepping back up" in t for t in sup.transitions)
        # Earned back only by staying up, not by starting.
        assert wait_for(lambda: sup._promote_wait == capture.PROMOTE_AFTER)
    finally:
        sup.stop()


def test_a_failed_step_up_waits_longer_before_the_next(world):
    sup = supervisor()
    try:
        world.blocked = {"printwindow"}
        world.dark = {"wgc"}
        assert wait_for(lambda: sup.backend is not None and sup.backend.kind is Backend.MSS)
        # WGC still starts and still produces nothing: each try falls back
        # to mss, and the wait before the next one doubles up to the cap.
        assert wait_for(lambda: sup._promote_wait == capture.PROMOTE_MAX, timeout=8)
        tries = sum("stepping back up" in t for t in sup.transitions)
        assert 2 <= tries <= 4
        # and stays at the cap
        assert wait_for(lambda: sum("stepping back up" in t for t in sup.transitions) > tries,
                        timeout=3)
        assert sup._promote_wait == capture.PROMOTE_MAX
    finally:
        sup.stop()


def test_a_top_backend_that_starts_and_soon_dies_does_not_earn_the_wait_back(world):
    # WGC comes up and produces briefly after each step up, then stops. That
    # is not recovery, and must not reset the wait to its shortest.
    sup = supervisor()
    try:
        world.blocked = {"printwindow"}
        world.lives = {"wgc": 0.1}
        assert wait_for(lambda: sup.backend is not None and sup.backend.kind is Backend.MSS)
        assert wait_for(lambda: sup._promote_wait == capture.PROMOTE_MAX, timeout=8)
    finally:
        sup.stop()


def test_the_top_backend_is_never_stepped_away_from(world):
    sup = supervisor()
    try:
        time.sleep(0.8)
        assert sup.backend.kind is Backend.WGC
        assert world.started == ["wgc"]
    finally:
        sup.stop()


def test_a_forced_backend_is_never_stepped_away_from(world):
    sup = supervisor(forced=Backend.MSS)
    try:
        time.sleep(0.8)
        assert world.started == ["mss"]
    finally:
        sup.stop()


def test_retry_starts_from_the_top_even_after_landing_lower(world):
    # WGC goes dark; failover lands on mss (PrintWindow will not start); then
    # mss dies too. The retry must go back to WGC, not resume at mss.
    sup = supervisor()
    try:
        world.blocked = {"printwindow"}
        world.dark = {"wgc"}
        assert wait_for(lambda: sup.backend is not None and sup.backend.kind is Backend.MSS)
        world.dark = set()
        world.blocked = {"printwindow", "mss"}
        sup.backend.error = "died"
        assert wait_for(lambda: sup.backend is not None and sup.backend.kind is Backend.WGC)
    finally:
        sup.stop()


def test_retries_keep_going_while_the_target_stays_dark(world):
    sup = supervisor()
    try:
        world.can_start = False
        world.producing = False
        assert wait_for(lambda: sum("retrying" in t for t in sup.transitions) >= 3)
        assert sup.backend is None
    finally:
        sup.stop()


def test_backoff_grows_and_resets_after_a_success(world):
    sup = supervisor()
    try:
        world.can_start = False
        world.producing = False
        assert wait_for(lambda: sum("retrying" in t for t in sup.transitions) >= 3)
        delays = [t for t in sup.transitions if "retrying" in t]
        assert delays[:3] == ["all backends failed; retrying in 0.1s",
                              "all backends failed; retrying in 0.2s",
                              "all backends failed; retrying in 0.2s"]
        world.can_start = True
        world.producing = True
        assert wait_for(lambda: sup.backend is not None)
        assert sup._retries == 0
    finally:
        sup.stop()


def test_a_forced_backend_is_restarted_not_replaced(world):
    sup = supervisor(forced=Backend.PRINTWINDOW)
    try:
        world.producing = False
        assert wait_for(lambda: world.started.count("printwindow") >= 2)
        assert set(world.started) == {"printwindow"}
    finally:
        sup.stop()


def test_stop_ends_the_retry_loop(world):
    sup = supervisor()
    world.can_start = False
    world.producing = False
    assert wait_for(lambda: any("retrying" in t for t in sup.transitions))
    sup.stop()
    assert not sup._thread.is_alive()
    world.can_start = True
    time.sleep(0.4)
    assert sup.backend is None


def test_transition_history_is_bounded(world):
    sup = supervisor()
    try:
        world.can_start = False
        world.producing = False
        for _ in range(200):
            sup._schedule_retry(RuntimeError("x"))
        assert len(sup.transitions) == capture.TRANSITIONS_KEPT
    finally:
        sup.stop()


# --- GL041-I01 / I03 (Codex) ---------------------------------------------------------


def test_a_start_that_outlasts_stop_is_not_left_running(world):
    """stop() joins the supervisor for 1 s; a start still running after that
    must not install a live backend nobody supervises."""
    sup = supervisor()
    world.slow = {"printwindow": 1.6}
    world.dark = {"wgc"}
    assert wait_for(lambda: any(i.kind is Backend.PRINTWINDOW for i in world.instances))
    sup.stop()                                   # returns while printwindow starts
    time.sleep(1.2)
    assert sup.backend is None
    late = [i for i in world.instances if i.kind is Backend.PRINTWINDOW][0]
    assert late.retired


def test_hung_teardowns_stop_new_starts_until_they_return(world):
    import threading

    world.hang = threading.Event()
    world.blocked = {"printwindow", "mss"}
    world.dark = {"wgc"}
    sup = supervisor()
    try:
        assert wait_for(lambda: world.started.count("wgc") >= capture.MAX_PENDING_TEARDOWNS)
        time.sleep(1.0)                          # several retry periods
        assert world.started.count("wgc") == capture.MAX_PENDING_TEARDOWNS
        world.hang.set()                         # the stuck stops return
        assert wait_for(lambda: world.started.count("wgc") > capture.MAX_PENDING_TEARDOWNS)
    finally:
        world.hang.set()
        sup.stop()
