"""Frame acquisition: buffer pool, backends, and the supervisor that swaps them.

Three properties this module has to hold and which are easy to get wrong:

* **Nobody overwrites a frame someone is still reading.** Publishing a reference
  to one reused buffer is a data race -- the next capture repaints the pixels
  while a consumer is mid-JPEG. Buffers are pooled and refcounted instead.
* **A duplicate delivery is not a new frame.** windows-capture 1.4.2 invokes the
  frame handler twice for the same frame when the row pitch is padded, so frames
  are deduplicated on the native timespan.
* **A blocked backend cannot wedge the pipeline.** PrintWindow is serviced by the
  target application and may never return, so it runs in a killable helper
  process supervised from outside, and results from a retired session are fenced
  off rather than published late.
"""

from __future__ import annotations

import itertools
import logging
import multiprocessing as mp
import threading
import time
from dataclasses import dataclass, field
from enum import Enum

import numpy as np

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

# Identity is verified on every published frame. An earlier revision throttled
# the enumerating half to 4Hz on cost grounds, which was wrong twice over: the
# per-frame remainder checked only IsWindow -- not even the title, contrary to
# its own comment -- and an ownership change reversed inside the interval
# escaped detection entirely. The cost was also misattributed; the crash that
# prompted the throttle came from building a second capture session, not from
# enumerating windows.

# How long a backend may go without producing a distinct frame before it is
# considered unhealthy and replaced.
FRAME_DEADLINE = 0.5

# How long a freshly activated backend may go before its *first* frame. Without
# this a backend whose very first call never returns stays "still starting up"
# forever, and the supervisor that exists to replace it never fires.
FIRST_FRAME_DEADLINE = 2.0


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
            self.last_frame_at = frame.captured_at
        return True


class WgcBackend(CaptureBackend):
    """Windows Graphics Capture via windows-capture.

    Binds by window *title* because 1.4.2 exposes no HWND selector and no way to
    read back the HWND it bound. Identity is therefore evidence, not proof: we
    require sole title ownership before binding and re-verify on every frame, and
    any failure stops capture rather than degrading quietly.
    """

    kind = Backend.WGC

    def __init__(self, binding, pool, sink) -> None:
        super().__init__(binding, pool, sink)
        self._control = None
        self._checked_first_frame = False
        self.border_suppressed = False

    def start(self) -> None:
        self.binding.verify(require_title_ownership=True)
        self.border_suppressed = WGC_BORDER_TOGGLE_SUPPORTED
        if not WGC_BORDER_TOGGLE_SUPPORTED:
            log.info(
                "WGC capture border cannot be hidden before Windows 11 "
                "(build %d); capturing with the default border",
                _sys.getwindowsversion().build,
            )
        self._control = self._open(
            draw_border=False if WGC_BORDER_TOGGLE_SUPPORTED else None
        )

    def _open(self, *, draw_border: bool | None):
        from windows_capture import WindowsCapture

        cap = WindowsCapture(
            cursor_capture=False,
            draw_border=draw_border,
            window_name=self.binding.title,
        )

        @cap.event
        def on_frame_arrived(frame, capture_control):  # noqa: ANN001
            try:
                if self.retired:
                    capture_control.stop()
                    return
                self._on_frame(frame)
            except IdentityLost as exc:
                self.error = str(exc)
                log.error("WGC identity check failed: %s", exc)
                capture_control.stop()
            except Exception as exc:                      # pragma: no cover
                self.error = repr(exc)
                log.exception("WGC frame handler failed")
                capture_control.stop()

        @cap.event
        def on_closed():                                   # noqa: ANN001
            log.info("WGC session closed")

        return cap.start_free_threaded()

    def _on_frame(self, frame) -> None:
        array = frame.frame_buffer
        height, width = array.shape[:2]

        if not self._checked_first_frame:
            # First frame is where a wrong binding is cheapest to catch.
            if not self.binding.dimensions_match(width, height):
                raise IdentityLost(
                    f"first frame is {width}x{height}, which matches neither the "
                    f"target's frame nor its client rect; refusing to trust this binding"
                )
            self._checked_first_frame = True

        self.binding.verify(require_title_ownership=True)
        self._publish(array, getattr(frame, "timespan", 0) or 0)

    def stop(self) -> None:
        self.retire()
        ctl = self._control
        if ctl is not None:
            try:
                ctl.stop()
            except Exception:
                log.debug("WGC stop raised", exc_info=True)
            self._control = None


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


class PrintWindowBackend(CaptureBackend):
    """PrintWindow, driven from a child process so it can be killed.

    HWND-exact: unlike WGC it addresses the window handle directly, so it is the
    backend to use when target identity has to be certain rather than merely
    evidenced.
    """

    kind = Backend.PRINTWINDOW

    def __init__(self, binding, pool, sink, *, max_pixels: int = 3840 * 2160) -> None:
        super().__init__(binding, pool, sink)
        self._max_bytes = max_pixels * 4
        self._shm = None
        self._ctrl = None
        self._proc: mp.Process | None = None
        self._reader: threading.Thread | None = None
        self._stop = threading.Event()
        self.torn_reads = 0

    def start(self) -> None:
        from multiprocessing.shared_memory import SharedMemory

        from gamelens import _pw_worker as worker

        self.binding.verify(require_title_ownership=False)

        self._shm = SharedMemory(create=True, size=self._max_bytes)
        self._ctrl = mp.Array("i", worker.CTRL_SIZE, lock=False)
        self._proc = mp.Process(
            target=worker.run,
            args=(self.binding.hwnd, self._shm.name, self._ctrl),
            name="gamelens-printwindow",
            daemon=True,
        )
        self._proc.start()

        self._reader = threading.Thread(
            target=self._read_loop, name="gamelens-pw-reader", daemon=True
        )
        self._reader.start()

    def _read_loop(self) -> None:
        from gamelens import _pw_worker as worker

        seen = 0
        while not self._stop.is_set() and not self.retired:
            try:
                proc = self._proc
                if proc is not None and not proc.is_alive() and proc.exitcode is not None:
                    # Windows spawns rather than forks, so the child re-imports
                    # the parent's __main__. A caller whose entry point is not
                    # guarded by `if __name__ == "__main__":` makes that import
                    # re-run their script, and multiprocessing refuses to start.
                    # Say so, rather than letting this look like a stalled game.
                    self.error = (
                        f"printwindow worker exited immediately (code {proc.exitcode}). "
                        f"On Windows the child re-imports the calling module: run "
                        f"GameLens via `python -m gamelens`, or guard your entry "
                        f"point with `if __name__ == \"__main__\":`."
                    )
                    return

                if self._ctrl[worker.CTRL_ERROR]:
                    self.error = "printwindow worker reported a fatal error"
                    return

                counter = self._ctrl[worker.CTRL_COUNTER]
                if counter == seen:
                    time.sleep(0.004)
                    continue

                snapshot = self._read_settled_frame()
                if snapshot is None:
                    continue                      # writer was mid-frame; try again
                array, counter = snapshot
                seen = counter

                self.binding.verify(require_title_ownership=False)
                # No row flip. GetBitmapBits on the compatible bitmap the worker
                # builds returns rows top-down already; the bottom-up assumption
                # this used to make produced a perfectly stable, perfectly
                # upside-down picture. Caught only by looking at a frame -- the
                # frame rate, the pool counters and the identity checks were all
                # happy with it.
                self._publish(array, counter)
            except IdentityLost as exc:
                self.error = str(exc)
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
        from gamelens import _pw_worker as worker

        for _ in range(attempts):
            before = self._ctrl[worker.CTRL_VERSION]
            if before % 2:
                time.sleep(0.001)
                continue

            width = self._ctrl[worker.CTRL_WIDTH]
            height = self._ctrl[worker.CTRL_HEIGHT]
            counter = self._ctrl[worker.CTRL_COUNTER]
            if width <= 0 or height <= 0 or width * height * 4 > self._max_bytes:
                return None

            nbytes = width * height * 4
            # Copy, not a view: the shared buffer keeps moving underneath.
            array = np.frombuffer(
                self._shm.buf[:nbytes], dtype=np.uint8
            ).reshape(height, width, 4).copy()

            if self._ctrl[worker.CTRL_VERSION] == before:
                return array, counter

        self.torn_reads += 1
        return None

    def stop(self) -> None:
        """Retire and kill. Never blocks on a call that may never return.

        The child is terminated rather than asked politely first: the whole
        reason it is a process is that it may be stuck inside PrintWindow, where
        a cooperative stop flag would never be read.
        """
        self.retire()
        self._stop.set()

        proc = self._proc
        if proc is not None and proc.is_alive():
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
        self.transitions: list[str] = []

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
        for i in range(index, len(self._order)):
            kind = self._order[i]
            cls = _BACKEND_CLASSES[kind]

            if kind is Backend.WGC and not title_still_owned_by(
                self.binding.hwnd, self.binding.title
            ):
                # Not an error: WGC binds by title, and the title is either
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
                self._backend = backend
                self._index = i
            self.transitions.append(f"{kind.value}: active (session {backend.session_id})")
            log.info("capture backend %s active (session %d)", kind.value, backend.session_id)
            return

        raise RuntimeError(
            f"no capture backend could start for hwnd {self.binding.hwnd}"
            + (f"; last error {last_error!r}" if last_error else "")
        )

    def _supervise(self) -> None:
        while not self._stop.wait(0.05):
            backend = self.backend
            if backend is None or backend.healthy(self.deadline):
                continue

            reason = backend.error or f"no frame for >{self.deadline}s"
            self.transitions.append(f"{backend.kind.value}: unhealthy ({reason})")
            log.error("backend %s unhealthy: %s", backend.kind.value, reason)

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
            threading.Thread(
                target=backend.stop, name="gamelens-backend-teardown", daemon=True
            ).start()

            if self._forced:
                log.error("backend forced to %s; not failing over", self._forced.value)
                return
            try:
                self._activate_from(self._index + 1)
            except RuntimeError as exc:
                log.error("failover exhausted: %s", exc)
                return

    def stop(self) -> None:
        self._stop.set()
        t = self._thread
        if t:
            t.join(timeout=1.0)
        backend = self.backend
        if backend:
            backend.stop()
        self.frames.clear()

    def stats(self) -> dict:
        backend = self.backend
        frame = self.frames.acquire()
        try:
            return {
                "backend": backend.kind.value if backend else "none",
                "session_id": backend.session_id if backend else 0,
                "healthy": backend.healthy(self.deadline) if backend else False,
                "distinct": backend.distinct if backend else 0,
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
