"""Frame acquisition: buffer pool, backends, and the supervisor that swaps them.

Three properties this module has to hold and which are easy to get wrong:

* **Nobody overwrites a frame someone is still reading.** Publishing a reference
  to one reused buffer is a data race -- the next capture repaints the pixels
  while a consumer is mid-JPEG. Buffers are pooled and refcounted instead.
* **A duplicate delivery is not a new frame.** windows-capture 1.4.2 invokes the
  frame handler twice for the same frame when the row pitch is padded, so frames
  are deduplicated on the native timespan.
* **A blocked or crashing backend cannot take GameLens with it.** PrintWindow is
  serviced by the target application and may never return; windows-capture can
  fault natively when a WGC session restarts. Both run in killable helper
  processes supervised from outside, and results from a retired session are
  fenced off rather than published late.
"""

from __future__ import annotations

import itertools
import logging
import multiprocessing as mp
import threading
from collections import deque
import time
from dataclasses import dataclass, field
from enum import Enum

import numpy as np

from gamelens import _frame_shm
from gamelens.windows import WindowInfo, describe, is_alive, title_still_owned_by

log = logging.getLogger(__name__)

_session_ids = itertools.count(1)
_frame_ids = itertools.count(1)


class Backend(Enum):
    WGC = "wgc"
    PRINTWINDOW = "printwindow"
    MSS = "mss"


FALLBACK_ORDER = (Backend.WGC, Backend.PRINTWINDOW, Backend.MSS)

# WGC's "hide the yellow capture border" toggle exists only on Windows 11.
# Asking for it on Windows 10 throws when the session starts -- and recovering by
# building a second session on the same window crashed the process outright, so
# the capability is decided up front rather than discovered by failing.
import sys as _sys
WGC_BORDER_TOGGLE_SUPPORTED = (
    _sys.platform.startswith("win") and _sys.getwindowsversion().build >= 22000
)

_wgc_hwnd: bool | None = None


def wgc_selects_by_hwnd() -> bool | None:
    """Whether the installed windows-capture can bind a window by its HWND.

    2.0 added ``window_hwnd`` (and turned ``window_name`` into a substring match,
    so an HWND-capable library is never asked by title). 1.4.2, the pinned
    version, binds by title only (README). Decided once, from the signature --
    importing the library here is harmless; only its sessions run in a worker.

    None when the library cannot be imported or inspected: unknown is not
    "legacy" (RV03-I02, Codex). Taken as legacy, a later successful import of
    2.x would be asked by title -- a substring match -- and could bind
    "Minecraft Launcher" for "Minecraft". Unknown is not cached, and WGC refuses
    to start on it.
    """
    global _wgc_hwnd
    if _wgc_hwnd is None:
        try:
            import inspect

            from windows_capture import WindowsCapture
            _wgc_hwnd = "window_hwnd" in inspect.signature(WindowsCapture).parameters
        except Exception:
            log.warning("cannot tell whether windows-capture binds by HWND", exc_info=True)
            return None
    return _wgc_hwnd


_graphics_capture_pinned = False


def pin_graphics_capture() -> bool:
    """Keep GraphicsCapture.dll loaded for the life of the process.

    Found on the Hyper-V VM with windows-capture 2.0.1: when a session ends and
    no other WGC object is alive, COM may unload the DLL while the ending
    session's callback thread is still running in it -- an access violation in
    "GraphicsCapture.dll_unloaded" that kills the whole process, with no Python
    traceback (2 of 7 Java world reloads, each of which starves WGC and forces a
    restart). Pinning makes that unload impossible. It did not make 2.0.1 safe:
    the next reload faulted inside windows_capture.pyd instead -- and 1.4.2 later
    did the same (GL-044), which is why WGC now runs in a worker process
    (``_wgc_worker``). Cheap, and it closes one way for that worker to die.
    Done once per process, before the first session; a failure is logged and
    capture proceeds as before.
    """
    global _graphics_capture_pinned
    if _graphics_capture_pinned or not _sys.platform.startswith("win"):
        return _graphics_capture_pinned
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.LoadLibraryW.restype = wintypes.HMODULE
        kernel32.GetModuleHandleExW.argtypes = (
            wintypes.DWORD, wintypes.LPCWSTR, ctypes.POINTER(wintypes.HMODULE))
        if not kernel32.LoadLibraryW("GraphicsCapture.dll"):
            raise ctypes.WinError(ctypes.get_last_error())
        GET_MODULE_HANDLE_EX_FLAG_PIN = 0x1
        module = wintypes.HMODULE()
        if not kernel32.GetModuleHandleExW(
                GET_MODULE_HANDLE_EX_FLAG_PIN, "GraphicsCapture.dll", ctypes.byref(module)):
            raise ctypes.WinError(ctypes.get_last_error())
        _graphics_capture_pinned = True
    except Exception:
        log.warning("could not pin GraphicsCapture.dll; a WGC restart may crash the worker",
                    exc_info=True)
    return _graphics_capture_pinned

# Identity is verified on every published frame. An earlier revision throttled
# the enumerating half to 4Hz on cost grounds, which was wrong twice over: the
# per-frame remainder checked only IsWindow -- not even the title, contrary to
# its own comment -- and an ownership change reversed inside the interval
# escaped detection entirely. The cost was also misattributed; the crash that
# prompted the throttle came from building a second capture session, not from
# enumerating windows.

# How many publication timestamps to keep for the rate. Two seconds at 60fps,
# so the figure reacts quickly without a poller's aliasing.
PUBLISH_RATE_WINDOW = 120

# How long a backend may go without producing a distinct frame before it is
# considered unhealthy and replaced.
FRAME_DEADLINE = 0.5

# How long a freshly activated backend may go before its *first* frame. Without
# this a backend whose very first call never returns stays "still starting up"
# forever, and the supervisor that exists to replace it never fires.
FIRST_FRAME_DEADLINE = 2.0

# When every backend has failed, how long to wait before starting again from the
# top of the order. A game that stops presenting for a moment -- Bedrock
# generating a world draws nothing for seconds -- walks every backend past its
# deadline in turn, and capture used to end there for good with the game
# running normally a second later (GL-041). Growing, capped, never giving up.
RETRY_BACKOFF = (1.0, 2.0, 5.0)

# The transition history is for reading on the dashboard, and a retry loop now
# appends to it for as long as the target stays dark.
TRANSITIONS_KEPT = 64

# How long a fallback backend must have been running before the supervisor
# tries the top of the order again, and the ceiling that wait doubles up to
# while the better backend keeps failing.
PROMOTE_AFTER = 10.0
PROMOTE_MAX = 160.0

# Teardowns run on their own threads because a backend's stop can hang (WGC's
# native stop has). Each drop starts one, and with retries a hung stop would
# leave one more thread and native session behind every few seconds; past this
# many unfinished, no new backend is started until they return (GL041-I03).
MAX_PENDING_TEARDOWNS = 3

# How long stop() waits for the last backend's teardown before giving up on it.
STOP_TEARDOWN_WAIT = 2.0


# --- buffer pool ----------------------------------------------------------


class _Buffer:
    """A pooled BGRA image buffer with a refcount.

    The refcount is plain integer arithmetic, and it is mutated **only** while
    the owning pool's lock is held. An earlier version let the capture thread
    increment under the pool lock, consumers retain under the frame-slot lock,
    and releases decrement under no lock at all -- three different regimes for
    one counter, which loses updates and races the decision to recycle. A lost
    increment hands a live buffer back to the pool and the next capture paints
    over a frame someone is reading; a lost decrement strands it forever.
    """

    __slots__ = ("array", "refs", "shape", "generation")

    def __init__(self, height: int, width: int, generation: int) -> None:
        self.array = np.empty((height, width, 4), dtype=np.uint8)
        self.shape = (height, width)
        self.refs = 0
        self.generation = generation


class FramePool:
    """Bounded pool of reusable buffers, and the single owner of their refcounts.

    Bounded on purpose: under load something has to give, and for a control loop
    the right thing to drop is an *old frame*, not memory. Exhaustion is counted
    so the dashboard can show it rather than hiding it as mysterious latency.
    """

    def __init__(self, depth: int = 4) -> None:
        """
        Four was originally a guess. It is now a measurement: four concurrent
        HTTP clients pulling frames as fast as the server would serve them --
        1750 fetches in 8 seconds, on top of the poller and the geometry thread
        -- exhausted the pool zero times while capture advanced 575 frames.

        Leases are short by design; every consumer takes one, copies or encodes,
        and releases. What would actually require more depth is a consumer that
        *holds* a frame across something slow, and the one place that was
        tempted to -- the vision tier, across a model call -- deliberately does
        not. Raise it if you add one that does; `exhausted` in the dashboard is
        how you would know.
        """
        self.depth = depth
        self._free: list[_Buffer] = []
        self._live = 0
        self._shape: tuple[int, int] | None = None
        self._generation = 0
        # Re-entrant: lease() holds it while taking the first reference.
        self._lock = threading.RLock()
        self.exhausted = 0
        self.double_release = 0

    def lease(self, height: int, width: int) -> _Buffer | None:
        with self._lock:
            if self._shape != (height, width):
                # A resize starts a new generation. Buffers from the old one are
                # still alive in whoever holds them; they simply never come back
                # here, and they must not disturb this generation's accounting.
                self._generation += 1
                self._free.clear()
                self._live = 0
                self._shape = (height, width)

            if self._free:
                buf = self._free.pop()
            elif self._live < self.depth:
                buf = _Buffer(height, width, self._generation)
                self._live += 1
            else:
                self.exhausted += 1
                return None

            buf.refs += 1
            return buf

    def retain(self, buf: _Buffer) -> None:
        with self._lock:
            if buf.refs <= 0:
                raise RuntimeError(
                    "cannot retain a released buffer: its pixels may already "
                    "have been overwritten by a later capture"
                )
            buf.refs += 1

    def release(self, buf: _Buffer) -> None:
        with self._lock:
            if buf.refs <= 0:
                # Never silently absorb this: it means someone released twice,
                # and the buffer may already be in use by a different frame.
                self.double_release += 1
                log.error("buffer released more times than it was retained")
                return

            buf.refs -= 1
            if buf.refs > 0:
                return

            # Exactly zero, decided under the same lock that made it zero, so
            # recycling happens once and only once.
            if buf.generation == self._generation and buf.shape == self._shape:
                self._free.append(buf)
            # Otherwise it belongs to a retired generation: let it be collected
            # without touching the current generation's counts.

    def stats(self) -> dict:
        with self._lock:
            return {"depth": self.depth, "free": len(self._free),
                    "live": self._live, "exhausted": self.exhausted,
                    "generation": self._generation,
                    "double_release": self.double_release}


@dataclass
class Frame:
    """A view onto a leased buffer. Release it, or use it as a context manager.

    ``array`` stays valid exactly as long as the lease is held. Holding a
    reference to it after ``release()`` is a use-after-free in slow motion: the
    pixels will change under you the next time that buffer is reused.
    """

    frame_id: int
    session_id: int
    backend: Backend
    captured_at: float
    native_timespan: int
    width: int
    height: int
    _buffer: _Buffer = field(repr=False)
    _pool: "FramePool" = field(repr=False)
    _released: bool = field(default=False, repr=False)

    @property
    def array(self) -> np.ndarray:
        if self._released:
            raise RuntimeError("frame released; its buffer may already be overwritten")
        return self._buffer.array

    def age(self) -> float:
        return time.monotonic() - self.captured_at

    def retain(self) -> "Frame":
        self._pool.retain(self._buffer)
        return Frame(
            self.frame_id, self.session_id, self.backend, self.captured_at,
            self.native_timespan, self.width, self.height, self._buffer, self._pool,
        )

    def release(self) -> None:
        if not self._released:
            self._released = True
            self._pool.release(self._buffer)

    def __enter__(self) -> "Frame":
        return self

    def __exit__(self, *exc) -> None:
        self.release()


class LatestFrame:
    """Single-slot holder. Readers always get the newest frame, never a backlog.

    A queue would build a latency staircase under backpressure -- exactly the
    wrong shape for control, where a frame you are too late to act on has no
    value at all.
    """

    def __init__(self) -> None:
        self._frame: Frame | None = None
        self._lock = threading.Lock()
        self._arrived = threading.Condition(self._lock)

    def publish(self, frame: Frame) -> None:
        with self._lock:
            old, self._frame = self._frame, frame
            self._arrived.notify_all()
        if old is not None:
            old.release()

    def acquire(self, timeout: float | None = None) -> Frame | None:
        """Take a lease on the current frame. Caller must release it."""
        with self._lock:
            if self._frame is None and timeout:
                self._arrived.wait(timeout)
            return self._frame.retain() if self._frame else None

    def latest_id(self) -> int:
        """The id of the newest published frame, or 0 before the first one."""
        with self._lock:
            return self._frame.frame_id if self._frame else 0

    def acquire_at_least(self, min_id: int, timeout: float) -> Frame | None:
        """Lease the newest frame once its id reaches ``min_id``, or None.

        This is how a caller sees the world *after* an action rather than
        whatever happened to be newest: an action returns the id that was
        current when its injection finished, and a frame published later than
        that was at least captured after the input existed. None means the
        deadline passed first -- the caller is told so, never handed the stale
        picture it was trying to get past.
        """
        deadline = time.monotonic() + max(0.0, timeout)
        with self._lock:
            while self._frame is None or self._frame.frame_id < min_id:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._arrived.wait(remaining)
            return self._frame.retain()

    def clear(self) -> None:
        with self._lock:
            old, self._frame = self._frame, None
        if old is not None:
            old.release()


# --- identity -------------------------------------------------------------


class IdentityLost(RuntimeError):
    """The window we are capturing is no longer provably the one we targeted."""


@dataclass(frozen=True)
class TargetBinding:
    hwnd: int
    title: str
    width: int
    height: int

    @classmethod
    def from_window(cls, info: WindowInfo) -> "TargetBinding":
        return cls(info.hwnd, info.title, info.width, info.height)

    def verify(self, *, require_title_ownership: bool) -> None:
        """Raise unless this is still the window we meant.

        ``require_title_ownership`` is True only for title-binding backends
        (WGC). PrintWindow addresses the HWND directly, so a game that renames
        its own window mid-play is harmless there -- demanding title ownership
        would stop capture for no reason.
        """
        if not is_alive(self.hwnd):
            raise IdentityLost(f"hwnd {self.hwnd} is gone")
        if not require_title_ownership:
            # HWND-exact backends still confirm the handle is alive; a rename is
            # harmless to them because they never resolved through the title.
            return
        if not title_still_owned_by(self.hwnd, self.title):
            raise IdentityLost(
                f"title {self.title!r} is no longer owned solely by hwnd {self.hwnd}; "
                f"a title-binding capture could be watching a different window"
            )

    def dimensions_match(self, width: int, height: int) -> bool:
        current = describe(self.hwnd)
        return (width, height) in {
            (current.width, current.height),
            (current.client_width, current.client_height),
        }


# --- backends -------------------------------------------------------------


class CaptureBackend:
    """One activation of one capture method. Retired backends stay retired."""

    kind: Backend

    def __init__(self, binding: TargetBinding, pool: FramePool, sink: LatestFrame) -> None:
        self.binding = binding
        self.pool = pool
        self.sink = sink
        self.session_id = next(_session_ids)
        self.distinct = 0
        self.duplicates = 0
        # Publication timestamps, for the rate. Owned by the publisher because
        # a rate derived by a poller can only ever report the poller's own
        # frequency: GL-037 was /state reporting 16fps for a backend publishing
        # 48.7, because the sampler ran at 20Hz and counted its own iterations.
        # A deque here also has no cross-backend state to corrupt -- `distinct`
        # restarts at zero on a swap, so a delta spanning one goes negative.
        self._published_at: deque[float] = deque(maxlen=PUBLISH_RATE_WINDOW)
        self.last_frame_at = 0.0
        self.activated_at = time.monotonic()
        self.error: str | None = None
        self._retired = threading.Event()
        self._last_timespan: int | None = None
        # Publication and retirement must not interleave, or a publisher that
        # has already passed its retired check can write into the slot after the
        # replacement backend owns it.
        self._publish_lock = threading.Lock()

    # -- lifecycle to implement --
    def start(self) -> None: raise NotImplementedError
    def stop(self) -> None: raise NotImplementedError

    @property
    def retired(self) -> bool:
        return self._retired.is_set()

    def retire(self) -> None:
        # Taken under the publish lock so a publication already in flight either
        # completes before this returns or sees the retirement -- never lands
        # after the slot has been handed to a replacement.
        with self._publish_lock:
            self._retired.set()

    def healthy(self, deadline: float = FRAME_DEADLINE) -> bool:
        if self.error or self.retired:
            return False
        if self.last_frame_at == 0.0:
            # Starting up -- but on a clock. "No frame yet" is exactly what a
            # backend blocked inside its first call looks like.
            return (time.monotonic() - self.activated_at) <= FIRST_FRAME_DEADLINE
        return (time.monotonic() - self.last_frame_at) <= deadline

    def publish_rate(self) -> float:
        """Frames published per second over the recent window.

        Frames *published*, which is not the same as distinct scenes: only the
        WGC backend carries a native timespan that `_publish` can deduplicate
        against, so for mss and PrintWindow every capture attempt publishes even
        when the pixels are identical. This number is honest about throughput
        and says nothing about content.
        """
        times = list(self._published_at)
        if len(times) < 2:
            return 0.0
        span = times[-1] - times[0]
        return (len(times) - 1) / span if span > 0 else 0.0

    def _publish(self, array: np.ndarray, timespan: int) -> bool:
        """Copy into a pooled buffer and publish. Returns False if dropped.

        Two gates before anything is published: a retired session may not write
        at all (its late result would clobber the replacement backend's frames),
        and a repeated native timespan is a duplicate delivery, not a new frame.
        """
        if self.retired:
            return False

        if timespan and timespan == self._last_timespan:
            self.duplicates += 1
            return False
        self._last_timespan = timespan

        height, width = array.shape[:2]
        buf = self.pool.lease(height, width)
        if buf is None:
            return False                     # pool exhausted: drop, already counted

        np.copyto(buf.array, array)
        frame = Frame(
            frame_id=next(_frame_ids),
            session_id=self.session_id,
            backend=self.kind,
            captured_at=time.monotonic(),
            native_timespan=timespan,
            width=width,
            height=height,
            _buffer=buf,
            _pool=self.pool,
        )
        # Publishing transfers this lease to the holder. The check and the
        # publish are one atomic step: separated, a publisher could pass the
        # check, pause, and write into a slot the supervisor has since cleared
        # for the replacement backend.
        with self._publish_lock:
            if self.retired:
                frame.release()
                return False
            self.sink.publish(frame)
            self.distinct += 1
            self._published_at.append(frame.captured_at)
            self.last_frame_at = frame.captured_at
        return True


class MssBackend(CaptureBackend):
    """Full-screen grab cropped to the window rect. Last resort.

    Unsafe if the window is occluded -- it captures whatever is on screen at
    those coordinates, which may be another window entirely. Used only when both
    better backends are unavailable.
    """

    kind = Backend.MSS

    def __init__(self, binding, pool, sink) -> None:
        super().__init__(binding, pool, sink)
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def start(self) -> None:
        self.binding.verify(require_title_ownership=False)
        self._thread = threading.Thread(target=self._loop, name="gamelens-mss", daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        import mss

        counter = itertools.count(1)
        with mss.mss() as sct:
            while not self._stop.is_set() and not self.retired:
                try:
                    self.binding.verify(require_title_ownership=False)
                    info = describe(self.binding.hwnd)
                    region = {"left": info.left, "top": info.top,
                              "width": info.width, "height": info.height}
                    shot = sct.grab(region)
                    array = np.frombuffer(shot.rgb, dtype=np.uint8).reshape(
                        shot.height, shot.width, 3)
                    bgra = np.dstack([array[:, :, ::-1],
                                      np.full(array.shape[:2], 255, np.uint8)])
                    self._publish(bgra, next(counter))
                except IdentityLost as exc:
                    self.error = str(exc)
                    return
                except Exception as exc:                  # pragma: no cover
                    self.error = repr(exc)
                    return
                time.sleep(0.01)

    def stop(self) -> None:
        self.retire()
        self._stop.set()
        t = self._thread
        if t:
            t.join(timeout=1.0)
        self._thread = None


_kill_job = None
_kill_job_lock = threading.Lock()


def die_with_this_process(pid: int) -> bool:
    """Tie child *pid* to this process: when GameLens exits, however it exits, so does the child.

    ``daemon=True`` only covers a clean interpreter exit. Found on the Hyper-V VM:
    GameLens killed hard (Stop-Process) left its PrintWindow worker running with
    no parent, still holding the log file the next GameLens run needed. A job
    object with KILL_ON_JOB_CLOSE is closed by the kernel when this process dies,
    and that terminates every process in it -- including one blocked inside
    PrintWindow, which could never notice its parent had gone. One job for the
    process lifetime; its handle is deliberately never closed.

    The child runs for a moment before it is assigned; a kill landing in that
    moment is covered by the worker itself, which exits when it sees its parent
    gone (``_pw_worker.run``).
    """
    global _kill_job
    if not _sys.platform.startswith("win"):
        return False
    try:
        import win32api
        import win32con
        import win32job

        with _kill_job_lock:
            if _kill_job is None:
                job = win32job.CreateJobObject(None, "")
                info = win32job.QueryInformationJobObject(
                    job, win32job.JobObjectExtendedLimitInformation)
                info["BasicLimitInformation"]["LimitFlags"] |= (
                    win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE)
                win32job.SetInformationJobObject(
                    job, win32job.JobObjectExtendedLimitInformation, info)
                _kill_job = job
            child = win32api.OpenProcess(
                win32con.PROCESS_SET_QUOTA | win32con.PROCESS_TERMINATE, False, pid)
            try:
                win32job.AssignProcessToJobObject(_kill_job, child)
            finally:
                win32api.CloseHandle(child)
        return True
    except Exception:
        log.warning("could not tie child %d to this process; it may outlive a hard kill",
                    pid, exc_info=True)
        return False


class WorkerBackend(CaptureBackend):
    """A capture method run in a child process, read back through shared memory.

    Used for the methods that can take their process down with them: PrintWindow
    can block forever inside the target application, and windows-capture can
    crash natively when a session restarts. Either way the child is what is lost.
    The parent keeps every decision about the frames -- identity, deduplication,
    the deadline -- and kills a child that stops producing; it never waits on it.
    """

    #: seconds to let the child stop cleanly before it is terminated
    stop_grace = 0.0

    def __init__(self, binding, pool, sink, *, max_pixels: int | None = None) -> None:
        super().__init__(binding, pool, sink)
        self._max_bytes = (max_pixels or slot_pixels(binding)) * 4
        self._shm = None
        self._ctrl = None
        self._proc: mp.Process | None = None
        self._reader: threading.Thread | None = None
        self._stop = threading.Event()
        self.torn_reads = 0

    # -- what a subclass provides --
    def _worker(self):
        """(target, the arguments that come before the shared slot) for the child."""
        raise NotImplementedError

    def _check(self, array: np.ndarray) -> None:
        """Raise IdentityLost if this frame may not be published."""
        self.binding.verify(require_title_ownership=False)

    # -- lifecycle --
    def _spawn(self) -> None:
        from multiprocessing.shared_memory import SharedMemory

        # Any failure part-way leaves nothing behind: the supervisor drops a
        # backend whose start raised without stopping it, so a worker started
        # here and not cleaned up here would run unsupervised until exit, one
        # more per retry (RV04-I01, Codex).
        try:
            self._shm = SharedMemory(create=True, size=self._max_bytes)
            self._ctrl = mp.Array(_frame_shm.CTRL_TYPECODE, _frame_shm.CTRL_SIZE, lock=False)
            target, args = self._worker()
            self._proc = mp.Process(
                target=target,
                args=(*args, self._shm.name, self._ctrl),
                name=f"gamelens-{self.kind.value}",
                daemon=True,
            )
            self._proc.start()
            if not die_with_this_process(self._proc.pid):
                # An unprotected worker can outlive a hard kill holding our files
                # (RV03-I03, Codex): refuse this backend rather than run one.
                raise RuntimeError(
                    f"could not tie the {self.kind.value} worker to this process")

            reader = threading.Thread(
                target=self._read_loop, name=f"gamelens-{self.kind.value}-reader", daemon=True
            )
            reader.start()
            self._reader = reader                # only a started thread can be joined
        except BaseException:
            self.stop()
            raise

    def _exit_message(self, code: int) -> str:
        code &= 0xFFFFFFFF
        text = f"{self.kind.value} worker exited (code {code:#x})"
        if code == 0xC0000005:
            text += ": it crashed (access violation); GameLens itself is unaffected"
        elif self.distinct == 0:
            # Windows spawns rather than forks, so the child re-imports the
            # parent's __main__. A caller whose entry point is not guarded by
            # `if __name__ == "__main__":` makes that import re-run their script,
            # and multiprocessing refuses to start. Say so, rather than letting
            # this look like a stalled game.
            text += ('. On Windows the child re-imports the calling module: run '
                     'GameLens via `python -m gamelens`, or guard your entry point '
                     'with `if __name__ == "__main__":`.')
        return text

    def _read_loop(self) -> None:
        seen = 0
        while not self._stop.is_set() and not self.retired:
            try:
                error = self._ctrl[_frame_shm.CTRL_ERROR]
                if error:
                    what = _frame_shm.ERROR_TEXT.get(error, f"failed ({error})")
                    self.error = f"{self.kind.value} worker {what}"
                    return

                proc = self._proc
                if proc is not None and not proc.is_alive() and proc.exitcode is not None:
                    self.error = self._exit_message(proc.exitcode)
                    log.error("%s", self.error)
                    return

                counter = self._ctrl[_frame_shm.CTRL_COUNTER]
                if counter == seen:
                    time.sleep(0.004)
                    continue

                snapshot = self._read_settled_frame()
                if snapshot is None:
                    continue                      # writer was mid-frame; try again
                array, counter, timespan = snapshot
                seen = counter

                self._check(array)
                # No row flip. Both workers hand over rows top-down; the
                # bottom-up assumption PrintWindow once made produced a perfectly
                # stable, perfectly upside-down picture. Caught only by looking at
                # a frame -- the frame rate, the pool counters and the identity
                # checks were all happy with it.
                self._publish(array, timespan or counter)
            except IdentityLost as exc:
                self.error = str(exc)
                log.error("%s identity check failed: %s", self.kind.value, exc)
                return
            except Exception as exc:                      # pragma: no cover
                self.error = repr(exc)
                return

    def _read_settled_frame(self, attempts: int = 4):
        """Copy one whole frame out of shared memory, or nothing.

        Seqlock read: the version is odd while the worker is writing, and
        changes across any write. If it is odd, or it moved while we were
        copying, the bytes we have are a blend of two frames and are discarded.
        """
        ctrl = self._ctrl
        for _ in range(attempts):
            before = ctrl[_frame_shm.CTRL_VERSION]
            if before % 2:
                time.sleep(0.001)
                continue

            width = ctrl[_frame_shm.CTRL_WIDTH]
            height = ctrl[_frame_shm.CTRL_HEIGHT]
            counter = ctrl[_frame_shm.CTRL_COUNTER]
            timespan = ctrl[_frame_shm.CTRL_TIMESPAN]
            if width <= 0 or height <= 0 or width * height * 4 > self._max_bytes:
                return None

            nbytes = width * height * 4
            # Copy, not a view: the shared buffer keeps moving underneath.
            array = np.frombuffer(
                self._shm.buf[:nbytes], dtype=np.uint8
            ).reshape(height, width, 4).copy()

            if ctrl[_frame_shm.CTRL_VERSION] == before:
                return array, counter, timespan

        self.torn_reads += 1
        return None

    def stop(self) -> None:
        """Retire and kill. Never blocks on a call that may never return."""
        self.retire()
        self._stop.set()

        proc = self._proc
        if proc is not None and proc.is_alive():
            if self.stop_grace and self._ctrl is not None:
                self._ctrl[_frame_shm.CTRL_STOP] = 1
                proc.join(timeout=self.stop_grace)
            if proc.is_alive():
                proc.terminate()
                proc.join(timeout=1.0)           # bounded; terminate already sent
                if proc.is_alive():
                    proc.kill()
        self._proc = None

        reader = self._reader
        if reader is not None:
            reader.join(timeout=0.5)
        self._reader = None

        if self._shm is not None:
            try:
                self._shm.close()
                self._shm.unlink()
            except Exception:
                log.debug("shared memory cleanup raised", exc_info=True)
            self._shm = None


# The smallest frame slot a worker gets. Only the pages a frame actually touches
# are ever resident; the rest is address space.
MIN_SLOT_PIXELS = 3840 * 2160


def slot_pixels(binding) -> int:
    """How many pixels a worker's frame slot holds for *binding*.

    Sized from the target and the desktop, not a fixed 4K: WGC in-process had no
    limit at all, and a fixed slot turned a 5K window into ERR_TOO_BIG on both
    worker backends -- and a fall-through to mss, which can capture whatever
    covers the window (RV04-I02, Codex). The virtual screen covers a window
    later maximized or moved to the biggest monitor; the target's own size
    covers one larger than any monitor.
    """
    pixels = max(MIN_SLOT_PIXELS, int(binding.width) * int(binding.height))
    if _sys.platform.startswith("win"):
        try:
            import ctypes

            SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN = 78, 79
            metrics = ctypes.windll.user32.GetSystemMetrics
            pixels = max(pixels, metrics(SM_CXVIRTUALSCREEN) * metrics(SM_CYVIRTUALSCREEN))
        except Exception:
            log.debug("virtual screen size unavailable", exc_info=True)
    return pixels


class WgcBackend(WorkerBackend):
    """Windows Graphics Capture via windows-capture, in a child process.

    In a child because windows-capture can crash natively when a session
    restarts -- 2.0.1 on Java world reloads, and 1.4.2 too, when an Enhanced
    Session connect starved WGC and the supervisor tried it again (GL-044). That
    crash used to end GameLens; now it ends the worker and the supervisor fails
    over (``_wgc_worker``).

    Binds by HWND when the library can (2.0+), which makes it as exact as
    PrintWindow. On 1.4.2 it binds by window *title*, with no way to read back the
    HWND it bound; identity is then evidence, not proof: we require sole title
    ownership before binding and re-verify on every frame, and any failure stops
    capture rather than degrading quietly.
    """

    kind = Backend.WGC
    # windows-capture ends a session properly when asked; a worker that does not
    # manage it in this long is killed like any other.
    stop_grace = 0.5

    def __init__(self, binding, pool, sink, **kw) -> None:
        super().__init__(binding, pool, sink, **kw)
        self._checked_first_frame = False
        self.border_suppressed = False
        self.by_hwnd = wgc_selects_by_hwnd()

    def start(self) -> None:
        if self.by_hwnd is None:
            raise RuntimeError("windows-capture is unavailable or could not be inspected; "
                               "not binding WGC by title on a guess")
        self.binding.verify(require_title_ownership=not self.by_hwnd)
        self.border_suppressed = WGC_BORDER_TOGGLE_SUPPORTED
        if not WGC_BORDER_TOGGLE_SUPPORTED:
            log.info(
                "WGC capture border cannot be hidden before Windows 11 "
                "(build %d); capturing with the default border",
                _sys.getwindowsversion().build,
            )
        self._spawn()

    def _worker(self):
        from gamelens import _wgc_worker

        if self.by_hwnd:
            target = {"window_hwnd": self.binding.hwnd}
        else:
            target = {"window_name": self.binding.title}  # 1.4.2 only: 2.x matches substrings
        return _wgc_worker.run, (target, False if WGC_BORDER_TOGGLE_SUPPORTED else None)

    def _check(self, array: np.ndarray) -> None:
        height, width = array.shape[:2]
        if not self._checked_first_frame:
            # First frame is where a wrong binding is cheapest to catch.
            if not self.binding.dimensions_match(width, height):
                raise IdentityLost(
                    f"first frame is {width}x{height}, which matches neither the "
                    f"target's frame nor its client rect; refusing to trust this binding"
                )
            self._checked_first_frame = True
        self.binding.verify(require_title_ownership=not self.by_hwnd)


class PrintWindowBackend(WorkerBackend):
    """PrintWindow, driven from a child process so it can be killed.

    HWND-exact: unlike WGC it addresses the window handle directly, so it is the
    backend to use when target identity has to be certain rather than merely
    evidenced. Stopped by termination, never asked politely first: the whole
    reason it is a process is that it may be stuck inside PrintWindow, where a
    cooperative stop flag would never be read.
    """

    kind = Backend.PRINTWINDOW

    def start(self) -> None:
        self.binding.verify(require_title_ownership=False)
        self._spawn()

    def _worker(self):
        from gamelens import _pw_worker

        return _pw_worker.run, (self.binding.hwnd,)


_BACKEND_CLASSES = {
    Backend.WGC: WgcBackend,
    Backend.PRINTWINDOW: PrintWindowBackend,
    Backend.MSS: MssBackend,
}


class CaptureSupervisor:
    """Owns the deadline and the backend transitions.

    Deliberately a separate thread from any backend: a supervisor living inside
    the backend could not notice that the backend had stopped responding, which
    is the failure it exists to catch.
    """

    def __init__(
        self,
        target: WindowInfo,
        *,
        pool_depth: int = 4,
        deadline: float = FRAME_DEADLINE,
        order: tuple[Backend, ...] = FALLBACK_ORDER,
        forced: Backend | None = None,
    ) -> None:
        self.binding = TargetBinding.from_window(target)
        self.pool = FramePool(pool_depth)
        self.frames = LatestFrame()
        self.deadline = deadline
        self._order = (forced,) if forced else order
        self._forced = forced
        self._index = 0
        self._backend: CaptureBackend | None = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.transitions: deque[str] = deque(maxlen=TRANSITIONS_KEPT)
        self._retries = 0
        self._retry_at: float | None = None
        self._promote_wait = PROMOTE_AFTER
        self._teardowns: list[threading.Thread] = []

    @property
    def backend(self) -> CaptureBackend | None:
        with self._lock:
            return self._backend

    def start(self) -> None:
        self._activate_from(0)
        self._thread = threading.Thread(
            target=self._supervise, name="gamelens-capture-supervisor", daemon=True
        )
        self._thread.start()

    def _activate_from(self, index: int) -> None:
        """Bring up the first backend at or after *index* that starts cleanly."""
        last_error: Exception | None = None
        self._teardowns = [t for t in self._teardowns if t.is_alive()]
        if len(self._teardowns) >= MAX_PENDING_TEARDOWNS:
            raise RuntimeError(f"{len(self._teardowns)} backend teardowns have not returned; "
                               "not starting another capture on top of them")
        for i in range(index, len(self._order)):
            if self._stop.is_set():
                raise RuntimeError("capture is stopping")
            kind = self._order[i]
            cls = _BACKEND_CLASSES[kind]

            if kind is Backend.WGC and wgc_selects_by_hwnd() is not True and not title_still_owned_by(
                self.binding.hwnd, self.binding.title
            ):
                # Not an error: this WGC binds by title, and the title is either
                # ambiguous or has moved to another window, so this backend
                # simply is not usable for this target right now.
                self.transitions.append(f"{kind.value}: skipped (title not solely owned)")
                log.warning(
                    "skipping WGC: %r is not solely owned by hwnd %d",
                    self.binding.title, self.binding.hwnd,
                )
                continue

            try:
                backend = cls(self.binding, self.pool, self.frames)
                backend.start()
            except Exception as exc:
                last_error = exc
                self.transitions.append(f"{kind.value}: start failed ({exc!r})")
                log.warning("backend %s failed to start: %r", kind.value, exc)
                continue

            with self._lock:
                # Checked under the lock stop() reads the backend under
                # (GL041-I01): a start that outlasts stop()'s join must not
                # install a live, unsupervised backend after shutdown.
                stopping = self._stop.is_set()
                if not stopping:
                    self._backend = backend
                    self._index = i
            if stopping:
                backend.retire()
                self._reap(backend)
                raise RuntimeError("capture stopped while a backend was starting")
            self.transitions.append(f"{kind.value}: active (session {backend.session_id})")
            log.info("capture backend %s active (session %d)", kind.value, backend.session_id)
            return

        raise RuntimeError(
            f"no capture backend could start for hwnd {self.binding.hwnd}"
            + (f"; last error {last_error!r}" if last_error else "")
        )

    def _schedule_retry(self, exc: Exception) -> None:
        delay = RETRY_BACKOFF[min(self._retries, len(RETRY_BACKOFF) - 1)]
        self._retries += 1
        self._retry_at = time.monotonic() + delay
        self.transitions.append(f"all backends failed; retrying in {delay:g}s")
        log.error("failover exhausted: %s; retrying from the top in %gs", exc, delay)

    def _retry(self) -> None:
        self._retry_at = None
        try:
            self._activate_from(0)
        except RuntimeError as exc:
            self._schedule_retry(exc)
            return
        self._retries = 0

    def _drop(self, backend: CaptureBackend) -> None:
        """Take a backend out of service; nothing it does afterwards is seen."""
        # Retire *before* stopping. From this moment its publications are
        # fenced off, so a call that returns late -- after failover -- cannot
        # overwrite the replacement backend's frames while its own teardown
        # is still in progress.
        backend.retire()
        with self._lock:
            if self._backend is backend:
                # Stop advertising it immediately. Leaving _backend pointing
                # at a retired object lets an observation taken against it
                # keep passing the arbiter's session check during failover.
                self._backend = None
        self.frames.clear()
        self._reap(backend)

    def _reap(self, backend: CaptureBackend) -> None:
        t = threading.Thread(target=backend.stop, name="gamelens-backend-teardown",
                             daemon=True)
        self._teardowns.append(t)
        t.start()

    def _promote(self, backend: CaptureBackend) -> None:
        """Leave a fallback for the top of the order, if the top works again.

        Failover only ever moves down, and the bottom is mss -- which captures
        whatever is on screen over the window. A few seconds of a game not
        presenting used to leave it there for the life of the process
        (GL-041). The cost of trying is one gap no longer than the first-frame
        deadline, during which nothing is published and every action is
        refused as stale. Each try doubles the wait before the next; only a top
        backend that then stays healthy for PROMOTE_AFTER earns it back, since
        one that starts and dies straight away looks like success at start.
        """
        self._promote_wait = min(self._promote_wait * 2, PROMOTE_MAX)
        self.transitions.append(f"{backend.kind.value}: stepping back up to "
                                f"{self._order[0].value}")
        log.info("backend %s has been up %.0fs; trying %s again",
                 backend.kind.value, time.monotonic() - backend.activated_at,
                 self._order[0].value)
        self._drop(backend)
        try:
            self._activate_from(0)
        except RuntimeError as exc:
            self._schedule_retry(exc)

    def _supervise(self) -> None:
        while not self._stop.wait(0.05):
            backend = self.backend
            if backend is None:
                # Nothing is published while this lasts, so every observation
                # fails the arbiter's freshness check: dark, not unsafe.
                if self._retry_at is not None and time.monotonic() >= self._retry_at:
                    self._retry()
                continue
            if backend.healthy(self.deadline):
                up_for = time.monotonic() - backend.activated_at
                if self._index > 0 and up_for >= self._promote_wait:
                    self._promote(backend)
                elif self._index == 0 and up_for >= PROMOTE_AFTER:
                    self._promote_wait = PROMOTE_AFTER
                continue

            reason = backend.error or f"no frame for >{self.deadline}s"
            self.transitions.append(f"{backend.kind.value}: unhealthy ({reason})")
            log.error("backend %s unhealthy: %s", backend.kind.value, reason)
            self._drop(backend)

            # A forced order holds one backend, so this goes straight to the
            # retry, which restarts that same backend: forced means "never a
            # different one", not "never again".
            try:
                self._activate_from(self._index + 1)
            except RuntimeError as exc:
                self._schedule_retry(exc)

    def stop(self) -> None:
        self._stop.set()
        t = self._thread
        if t:
            t.join(timeout=1.0)
        with self._lock:
            backend, self._backend = self._backend, None
        if backend:
            backend.retire()
        self.frames.clear()
        if backend:
            # A native stop can hang (WGC's has); shutdown must still get past
            # it to the rest of the teardown (GL041-RV02-I02).
            self._reap(backend)
            self._teardowns[-1].join(timeout=STOP_TEARDOWN_WAIT)

    def stats(self) -> dict:
        backend = self.backend
        frame = self.frames.acquire()
        try:
            return {
                "backend": backend.kind.value if backend else "none",
                "session_id": backend.session_id if backend else 0,
                # Non-null only when failover is off -- the one case in which
                # the backend cannot change under a caller mid-measurement.
                "forced_backend": self._forced.value if self._forced else None,
                "healthy": backend.healthy(self.deadline) if backend else False,
                "distinct": backend.distinct if backend else 0,
                "publish_rate": backend.publish_rate() if backend else 0.0,
                "duplicates": backend.duplicates if backend else 0,
                "error": backend.error if backend else None,
                "frame_id": frame.frame_id if frame else 0,
                "width": frame.width if frame else 0,
                "height": frame.height if frame else 0,
                "age_ms": frame.age() * 1000 if frame else float("inf"),
                "pool": self.pool.stats(),
                "transitions": list(self.transitions),
            }
        finally:
            if frame:
                frame.release()
