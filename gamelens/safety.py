"""Interlocks that stand between an agent's intent and a real keystroke.

Design rule for everything here: **fail closed**. Each check must be
affirmatively true for an action to proceed, an exception anywhere inside the
guard denies, and no state defaults to permissive. The failure mode being
designed against is not "the agent is malicious" -- it is "something broke and
the system kept clicking anyway".
"""

from __future__ import annotations

import ctypes
import logging
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import Enum

import win32gui

log = logging.getLogger(__name__)

_user32 = ctypes.windll.user32

VK_F12 = 0x7B
VK_PAUSE = 0x13

# A heartbeat older than this means the watchdog is hung. It is *only* the
# detection bound for a thread that is alive but stuck: a thread that has
# actually died is caught immediately by the liveness check, because freshness
# alone would keep admitting actions for the whole window after it exits.
HEARTBEAT_MAX_AGE = 0.5
WATCHDOG_INTERVAL = 0.02


class Denial(Enum):
    OK = "ok"
    NOT_ARMED = "not armed"
    KILLED = "kill switch latched"
    NOT_FOREGROUND = "target window is not in the foreground"
    RATE_LIMITED = "action rate limit exceeded"
    WATCHDOG_DEAD = "safety watchdog thread is not running"
    WATCHDOG_STALE = "safety watchdog heartbeat is stale"
    TARGET_GONE = "target window no longer exists"
    GUARD_ERROR = "guard raised; denying by default"
    # Checked by the executor at each press rather than by check(): they are
    # about the input about to be sent, which check() never sees (GL-040).
    POINTER_OFF_TARGET = "the cursor is not over the target window"
    FOREIGN_MODIFIER = "a modifier GameLens did not press is held down"
    UNRELEASED_INPUT = "an earlier release failed; input may still be held"


class NotPermitted(RuntimeError):
    def __init__(self, reason: Denial, detail: str = "") -> None:
        self.reason = reason
        super().__init__(f"{reason.value}{(': ' + detail) if detail else ''}")


@dataclass
class TokenBucket:
    """Classic token bucket. Burst-tolerant, average-rate limited."""

    rate: float = 10.0
    capacity: float = 10.0
    _tokens: float = field(default=0.0, init=False)
    _last: float = field(default_factory=time.monotonic, init=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False)

    def __post_init__(self) -> None:
        self._tokens = self.capacity

    def take(self, amount: float = 1.0) -> bool:
        with self._lock:
            now = time.monotonic()
            self._tokens = min(self.capacity, self._tokens + (now - self._last) * self.rate)
            self._last = now
            if self._tokens >= amount:
                self._tokens -= amount
                return True
            return False

    def peek(self) -> float:
        with self._lock:
            now = time.monotonic()
            return min(self.capacity, self._tokens + (now - self._last) * self.rate)


class SafetySupervisor:
    """Owns arming, the kill latch, and the watchdog that proves it is alive."""

    def __init__(
        self,
        target_hwnd: int,
        *,
        rate: float = 10.0,
        dry_run: bool = True,
        kill_keys: tuple[int, ...] = (VK_F12, VK_PAUSE),
    ) -> None:
        self._hwnd = target_hwnd
        self._kill_keys = kill_keys
        self._lock = threading.Lock()

        # Serializes a *committed press* against kill/disarm. self._lock alone is
        # not enough: check() must release it to poll the foreground window and
        # the rate limiter, and a kill completing inside that gap would otherwise
        # be followed by an approved press -- after cleanup had already run, so
        # nothing would ever release it. Lock order everywhere is
        # _dispatch -> _lock, never the reverse.
        self._dispatch = threading.RLock()

        self._armed = False
        self._killed = False          # latched; only a restart clears it
        self._dry_run = dry_run
        self._kill_reason = ""

        self._bucket = TokenBucket(rate=rate, capacity=rate)

        # Starts invalid on purpose. Until the watchdog has actually beaten once,
        # there is no evidence it is running, and "no evidence" must not read as
        # "healthy".
        self._heartbeat: float | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

        self._on_kill: list = []

    # --- lifecycle --------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._watch, name="gamelens-watchdog", daemon=True
        )
        self._thread.start()
        # Wait for the first real heartbeat so callers never see the invalid state
        # as a spurious denial during startup.
        deadline = time.monotonic() + 2.0
        while self._heartbeat is None and time.monotonic() < deadline:
            time.sleep(0.005)
        if self._heartbeat is None:
            raise RuntimeError("safety watchdog failed to start")

    def shutdown(self) -> None:
        self._stop.set()
        t = self._thread
        if t is not None:
            t.join(timeout=1.0)
        self._thread = None

    def _watch(self) -> None:
        """Poll the kill keys and prove liveness.

        The `finally` is the important part: if this thread dies for *any*
        reason, it latches the system disarmed on the way out, so a crash here
        cannot leave injection enabled with a heartbeat that is still nominally
        fresh.
        """
        try:
            while not self._stop.is_set():
                for vk in self._kill_keys:
                    # High bit set == currently down.
                    if _user32.GetAsyncKeyState(vk) & 0x8000:
                        self.kill(f"hotkey vk={vk:#04x}")
                        break
                self._heartbeat = time.monotonic()
                time.sleep(WATCHDOG_INTERVAL)
        except BaseException:
            log.exception("safety watchdog died; latching disarmed")
            raise
        finally:
            with self._lock:
                self._armed = False
            if not self._stop.is_set():
                # Unplanned exit: treat it as a kill so nothing in flight proceeds.
                self.kill("watchdog exited unexpectedly")

    # --- operator controls ------------------------------------------------

    def arm(self) -> None:
        with self._lock:
            if self._killed:
                raise NotPermitted(Denial.KILLED, self._kill_reason)
            self._armed = True
        log.warning("ARMED (dry_run=%s)", self._dry_run)

    def disarm(self) -> None:
        """Stop authorizing new input, under the same boundary as a press.

        Taking only ``_lock`` here would leave the gap this boundary exists to
        close: a commit that had already passed its armed-state check could
        resume after ``disarm()`` returned and still press. Disarming does not
        release what is already held -- callers that need that should kill, or
        release explicitly.
        """
        with self._dispatch:
            with self._lock:
                self._armed = False
        log.warning("disarmed")

    @contextmanager
    def release_boundary(self):
        """Serialize a *release* against kill cleanup and against other releases.

        Deliberately carries no interlocks: a release must proceed even when
        every guard is denying, or a killed session leaves keys held down. What
        it does provide is ordering, so a normal key-up and a cleanup release can
        never both fire for the same key.
        """
        with self._dispatch:
            yield

    def go_live(self) -> None:
        """Leave dry-run. Deliberately separate from arming: two steps, not one."""
        with self._lock:
            if self._killed:
                raise NotPermitted(Denial.KILLED, self._kill_reason)
            if not self._armed:
                raise NotPermitted(Denial.NOT_ARMED, "arm before going live")
            self._dry_run = False
        log.warning("LIVE -- input will now be injected")

    def kill(self, reason: str = "operator") -> None:
        """Latch the system off. Irreversible for the life of the process.

        Held under the dispatch boundary so the ordering against a press is
        total: either a press commits first and is therefore tracked before
        cleanup enumerates what to release, or the latch lands first and the
        press is refused when it revalidates. There is no interleaving where
        cleanup completes and a press follows it.
        """
        with self._dispatch:
            with self._lock:
                if self._killed:
                    return
                self._killed = True
                self._armed = False
                self._dry_run = True
                self._kill_reason = reason
                callbacks = list(self._on_kill)
            log.error("KILL LATCHED: %s", reason)
            for cb in callbacks:
                try:
                    cb(reason)
                except Exception:
                    log.exception("kill callback failed")

    def on_kill(self, callback) -> None:
        """Register a cleanup hook -- used by the input executor to release keys."""
        with self._lock:
            self._on_kill.append(callback)

    # --- the guard --------------------------------------------------------

    def check(self, *, cost: float = 1.0, consume: bool = True) -> Denial:
        """Evaluate every interlock. Returns OK or the first failing reason.

        Any unexpected exception denies rather than propagating, so a bug in a
        check cannot become an unguarded action.
        """
        try:
            with self._lock:
                if self._killed:
                    return Denial.KILLED
                if not self._armed:
                    return Denial.NOT_ARMED

            thread = self._thread
            if thread is None or not thread.is_alive():
                return Denial.WATCHDOG_DEAD

            beat = self._heartbeat
            if beat is None or (time.monotonic() - beat) > HEARTBEAT_MAX_AGE:
                return Denial.WATCHDOG_STALE

            if not win32gui.IsWindow(self._hwnd):
                return Denial.TARGET_GONE
            if win32gui.GetForegroundWindow() != self._hwnd:
                return Denial.NOT_FOREGROUND

            if consume:
                if not self._bucket.take(cost):
                    return Denial.RATE_LIMITED
            elif self._bucket.peek() < cost:
                return Denial.RATE_LIMITED

            return Denial.OK
        except Exception:
            log.exception("safety guard raised; denying")
            return Denial.GUARD_ERROR

    def require(self, *, cost: float = 1.0) -> None:
        """Raise unless every interlock passes.

        Advisory only -- safe for read-only decisions such as whether to queue
        work. A press must use ``commit()`` instead, because this call's verdict
        stops being true the moment it returns.
        """
        verdict = self.check(cost=cost)
        if verdict is not Denial.OK:
            raise NotPermitted(verdict, self._kill_reason if verdict is Denial.KILLED else "")

    @contextmanager
    def commit(self, *, cost: float = 1.0):
        """Authorize and perform one press without a gap in between.

        Every interlock is evaluated *inside* the dispatch boundary and the body
        runs while it is still held, so a kill cannot land between the approval
        and the keystroke it approved. Anything slow -- dwells, model calls, the
        kill callbacks' own work -- must stay outside this block.
        """
        with self._dispatch:
            verdict = self.check(cost=cost)
            if verdict is not Denial.OK:
                raise NotPermitted(
                    verdict, self._kill_reason if verdict is Denial.KILLED else ""
                )
            yield

    # --- introspection ----------------------------------------------------

    @property
    def armed(self) -> bool:
        with self._lock:
            return self._armed

    @property
    def killed(self) -> bool:
        with self._lock:
            return self._killed

    @property
    def dry_run(self) -> bool:
        with self._lock:
            return self._dry_run

    @property
    def target_hwnd(self) -> int:
        return self._hwnd

    @property
    def rate_capacity(self) -> float:
        """The most actions the rate limiter can ever admit in one burst."""
        return self._bucket.capacity

    def snapshot(self) -> dict:
        with self._lock:
            beat = self._heartbeat
            thread = self._thread
            return {
                "armed": self._armed,
                "killed": self._killed,
                "dry_run": self._dry_run,
                "kill_reason": self._kill_reason,
                "watchdog_alive": bool(thread and thread.is_alive()),
                "heartbeat_age_ms": None if beat is None else (time.monotonic() - beat) * 1000,
                "tokens": self._bucket.peek(),
            }
