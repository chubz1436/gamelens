"""Synthetic overlap control checks; no live capture/input or real sleeps."""
from types import SimpleNamespace

import pytest

from gamelens.marathon import UnknownScreen
from gamelens.marathon_overlap import PortalExchangeOverlap


class Clock:
    def __init__(self):
        self.now = 100.0
        self.sleeps = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class Controller:
    def __init__(self, chapter=14, trainer=8):
        self.progress = SimpleNamespace(chapter=chapter)
        self.started_at = 123456
        self.trainer = trainer
        self.mounted = "mounted"
        self.portal_ready = self.ride_ready = True
        self.spawn = False
        self.actions = []
        self.waits = []
        self.reject_key = None
        self.reads = 0

    def read(self):
        self.reads += 1

    def area(self):
        return "city" if self.spawn or self.trainer == 8 else "suburb"

    def portrait_matches(self, trainer):
        return trainer == self.trainer

    def mount_state(self):
        return self.mounted

    def athens_portal_ready(self):
        return self.portal_ready

    def riding_ready(self):
        return self.ride_ready

    def at_athens_spawn(self):
        return self.spawn

    def act(self, steps, label):
        key = steps[0]["key"]
        self.actions.append(key)
        if key == self.reject_key:
            raise UnknownScreen("guard refused; no retry")
        if key == "7":
            self.mounted = "unmounted" if self.mounted == "mounted" else "mounted"

    def wait_mount(self, expected, seconds=12):
        self.waits.append(("mount", expected, seconds))
        if self.mounted != expected:
            raise UnknownScreen("mount timeout")

    def wait_for(self, test, seconds, description):
        self.waits.append(("predicate", seconds))
        self.read()
        if not test():
            raise UnknownScreen(description)


@pytest.fixture
def clock(monkeypatch):
    clock = Clock()
    monkeypatch.setattr("gamelens.marathon_overlap.time.monotonic", clock.monotonic)
    monkeypatch.setattr("gamelens.marathon_overlap.time.sleep", clock.sleep)
    return clock


@pytest.mark.parametrize("chapter,trainer", [(8, 1), (14, 8)])
def test_portal_dispatch_does_not_confirm_math_and_receipt_then_remounts(clock, chapter, trainer):
    c = Controller(chapter, trainer)
    overlap = PortalExchangeOverlap()
    before = overlap.before_exchange(c, chapter, trainer)
    assert before["portal_sent"] and not before["math_receipt_confirmed"]
    assert c.actions == ["7", "2"] and c.progress.chapter == chapter
    c.progress.chapter += 1  # caller's actual verified receipt
    c.spawn = True
    after = overlap.after_confirmed_exchange(c, chapter, trainer)
    assert after["phase"] == "completed" and after["math_receipt_confirmed"]
    assert c.actions == ["7", "2", "7"] and c.mounted == "mounted"
    assert c.started_at == 123456 and c.progress.chapter == chapter+1
    assert clock.sleeps == pytest.approx([2.3])


@pytest.mark.parametrize("math_seconds,remaining", [(1, 1.3), (3, 0)])
def test_only_verified_arrival_can_consume_loading_guard(clock, math_seconds, remaining):
    c = Controller()
    overlap = PortalExchangeOverlap()
    overlap.before_exchange(c, 14, 8)
    assert not overlap.observe_arrival(c) and overlap.arrival_at is None
    c.spawn = True
    assert overlap.observe_arrival(c)
    clock.now += math_seconds
    c.progress.chapter = 15
    result = overlap.after_confirmed_exchange(c, 14, 8)
    assert result["additional_guard_seconds"] == pytest.approx(remaining)
    assert clock.sleeps == pytest.approx([remaining] if remaining else [])
    assert result["cast_to_mounted_seconds"] == pytest.approx(max(math_seconds, 2.3))


def test_long_math_without_arrival_proof_still_waits_post_arrival_guard(clock):
    c = Controller()
    overlap = PortalExchangeOverlap()
    overlap.before_exchange(c, 14, 8)
    clock.now += 10
    c.spawn = True
    c.progress.chapter = 15
    overlap.after_confirmed_exchange(c, 14, 8)
    assert clock.sleeps == pytest.approx([2.3])


@pytest.mark.parametrize("refresh,extra_reads", [(True, 1), (False, 0)])
def test_arrival_observer_default_refreshes_but_read_hook_does_not(clock, refresh, extra_reads):
    c = Controller()
    overlap = PortalExchangeOverlap()
    overlap.before_exchange(c, 14, 8)
    c.spawn = True
    reads_before = c.reads
    if refresh:
        assert overlap.observe_arrival(c)  # default refresh=True
    else:
        assert overlap.observe_arrival(c, refresh=False)
    assert c.reads - reads_before == extra_reads
    assert overlap.arrival_at == clock.now


@pytest.mark.parametrize("chapter,trainer", [(7, 1), (8, 2), (14, 7), (15, 8)])
def test_unsupported_exchange_never_acts(clock, chapter, trainer):
    c = Controller(chapter, trainer)
    with pytest.raises(UnknownScreen, match="unsupported"):
        PortalExchangeOverlap().before_exchange(c, chapter, trainer)
    assert not c.actions


@pytest.mark.parametrize("failure", ["chapter", "portrait", "mounted", "portal"])
def test_before_origin_guards_refuse_before_dismount(clock, failure):
    c = Controller()
    if failure == "chapter":
        c.progress.chapter = 13
    elif failure == "portrait":
        c.trainer = 7
    elif failure == "mounted":
        c.mounted = "unmounted"
    else:
        c.portal_ready = False
    overlap = PortalExchangeOverlap()
    with pytest.raises(UnknownScreen):
        overlap.before_exchange(c, 14, 8)
    assert not c.actions and overlap.phase == "stopped"


def test_sent_portal_without_confirmed_exchange_cannot_remount(clock):
    c = Controller()
    overlap = PortalExchangeOverlap()
    overlap.before_exchange(c, 14, 8)
    c.spawn = True
    with pytest.raises(UnknownScreen, match="receipt"):
        overlap.after_confirmed_exchange(c, 14, 8)
    assert not overlap.math_receipt_confirmed and c.actions == ["7", "2"]
    assert c.progress.chapter == 14


@pytest.mark.parametrize("failure", ["warp", "riding", "remount"])
def test_post_receipt_failure_preserves_confirmed_chapter_and_no_retries(clock, failure):
    c = Controller()
    overlap = PortalExchangeOverlap()
    overlap.before_exchange(c, 14, 8)
    c.progress.chapter = 15
    c.spawn = failure != "warp"
    c.ride_ready = failure != "riding"
    if failure == "remount":
        c.reject_key = "7"
    with pytest.raises(UnknownScreen):
        overlap.after_confirmed_exchange(c, 14, 8)
    original_actions = list(c.actions)
    assert overlap.math_receipt_confirmed and c.progress.chapter == 15
    assert c.started_at == 123456 and overlap.phase == "stopped"
    with pytest.raises(UnknownScreen):
        overlap.after_confirmed_exchange(c, 14, 8)
    with pytest.raises(UnknownScreen):
        overlap.before_exchange(c, 14, 8)
    assert c.actions == original_actions


def test_wrong_controller_or_chapter_cannot_use_armed_helper(clock):
    c = Controller()
    overlap = PortalExchangeOverlap()
    overlap.before_exchange(c, 14, 8)
    for controller, chapter, trainer in [(Controller(), 14, 8), (c, 8, 1)]:
        with pytest.raises(UnknownScreen, match="matching"):
            overlap.after_confirmed_exchange(controller, chapter, trainer)
    assert c.actions == ["7", "2"]


def test_uncertain_portal_dispatch_stops_without_retry_or_receipt_claim(clock):
    c = Controller()
    c.reject_key = "2"
    overlap = PortalExchangeOverlap()
    with pytest.raises(UnknownScreen):
        overlap.before_exchange(c, 14, 8)
    assert c.actions == ["7", "2"] and not overlap.portal_sent
    assert not overlap.math_receipt_confirmed and overlap.phase == "stopped"
    with pytest.raises(UnknownScreen):
        overlap.before_exchange(c, 14, 8)
    assert c.actions == ["7", "2"]
