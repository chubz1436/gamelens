"""SendInput wrappers and the executor that is allowed to use them.

Two things in here are classic silent-failure sources and are asserted rather
than trusted:

* **The INPUT struct layout.** ``ctypes.wintypes`` has no ``ULONG_PTR``. Using
  ``DWORD`` for ``dwExtraInfo`` yields a 28-byte INPUT on 64-bit Python, and
  ``SendInput`` then rejects every event by returning 0 -- no exception, no
  traceback, just a harness that does nothing. ``sizeof(INPUT)`` is checked at
  import.
* **Absolute coordinate normalization.** With ``MOUSEEVENTF_VIRTUALDESK`` the
  normalized range spans the whole virtual desktop, whose origin is negative when
  a monitor sits left of or above the primary. Omitting the origin term puts every
  click on the wrong monitor while looking perfectly reasonable in code.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import logging
import queue
import threading
import time
from dataclasses import dataclass, field
from enum import Enum

from gamelens.coords import Geometry, VirtualDesktop, virtual_desktop
from gamelens.dpi import require_trustworthy_dpi
from gamelens.safety import Denial, NotPermitted, SafetySupervisor

log = logging.getLogger(__name__)

_user32 = ctypes.windll.user32

# ULONG_PTR is pointer-sized: 8 bytes on win64, 4 on win32. wintypes does not
# define it, and getting this wrong is the single most common SendInput bug.
ULONG_PTR = ctypes.c_uint64 if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_ulong

INPUT_MOUSE = 0
INPUT_KEYBOARD = 1

MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_MIDDLEDOWN = 0x0020
MOUSEEVENTF_MIDDLEUP = 0x0040
MOUSEEVENTF_WHEEL = 0x0800
MOUSEEVENTF_ABSOLUTE = 0x8000
MOUSEEVENTF_VIRTUALDESK = 0x4000

KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_SCANCODE = 0x0008

MAPVK_VK_TO_VSC = 0

# Keys that live on the "extended" part of the keyboard. Without the extended
# flag the scancode for, say, Right Arrow collides with numpad 6 and games read
# the wrong key.
_EXTENDED_VKS = frozenset({
    0x21, 0x22, 0x23, 0x24,        # PgUp PgDn End Home
    0x25, 0x26, 0x27, 0x28,        # Left Up Right Down
    0x2D, 0x2E,                    # Insert Delete
    0x2C,                          # PrintScreen
    0x90,                          # NumLock
    0xA3,                          # RControl
    0xA5,                          # RMenu (right Alt)
    0x6F,                          # Numpad divide
})


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wt.LONG),
        ("dy", wt.LONG),
        ("mouseData", wt.DWORD),
        ("dwFlags", wt.DWORD),
        ("time", wt.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wt.WORD),
        ("wScan", wt.WORD),
        ("dwFlags", wt.DWORD),
        ("time", wt.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wt.DWORD), ("wParamL", wt.WORD), ("wParamH", wt.WORD)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wt.DWORD), ("u", _INPUTUNION)]


_EXPECTED_INPUT_SIZE = 40 if ctypes.sizeof(ctypes.c_void_p) == 8 else 28
if ctypes.sizeof(INPUT) != _EXPECTED_INPUT_SIZE:  # pragma: no cover - import guard
    raise RuntimeError(
        f"INPUT struct is {ctypes.sizeof(INPUT)} bytes, expected {_EXPECTED_INPUT_SIZE}. "
        f"SendInput would silently reject every event. Check the ULONG_PTR definition."
    )

_user32.SendInput.argtypes = [wt.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
_user32.SendInput.restype = wt.UINT

_user32.GetCursorPos.argtypes = [ctypes.POINTER(wt.POINT)]
_user32.GetCursorPos.restype = wt.BOOL


class InjectionFailed(RuntimeError):
    """SendInput accepted fewer events than we handed it."""


def _send(events: list[INPUT]) -> None:
    """Hand events to the OS and verify the count it accepted.

    A short return is a real failure -- most often UIPI blocking injection into a
    window owned by a more privileged process. Ignoring it produces a harness that
    appears to work and does nothing.
    """
    if not events:
        return
    arr = (INPUT * len(events))(*events)
    sent = _user32.SendInput(len(events), arr, ctypes.sizeof(INPUT))
    if sent != len(events):
        err = ctypes.get_last_error() if ctypes.get_last_error() else None
        raise InjectionFailed(
            f"SendInput accepted {sent} of {len(events)} events"
            + (f" (GetLastError={err})" if err else "")
            + ". A zero count usually means UIPI blocked injection: the target window "
              "belongs to an elevated process and GameLens is not elevated."
        )


# --- coordinate normalization -------------------------------------------


def normalize_absolute(x: int, y: int, desktop: VirtualDesktop | None = None) -> tuple[int, int]:
    """Map a physical screen pixel to the 0..65535 virtual-desktop range.

    The origin term is not optional. On a desktop spanning x=-1920..1919,
    dropping it sends a click intended for x=0 to x=-1920 -- the far edge of the
    wrong monitor.
    """
    vd = desktop or virtual_desktop()
    if not vd.contains(x, y):
        raise ValueError(
            f"({x},{y}) is outside the virtual desktop "
            f"[{vd.left},{vd.top} {vd.width}x{vd.height}]; refusing to clamp it onto "
            f"a monitor the caller did not mean"
        )
    nx = round((x - vd.left) * 65535 / max(vd.width - 1, 1))
    ny = round((y - vd.top) * 65535 / max(vd.height - 1, 1))
    return int(nx), int(ny)


def _mouse_event(flags: int, nx: int = 0, ny: int = 0, data: int = 0) -> INPUT:
    ev = INPUT(type=INPUT_MOUSE)
    ev.mi = MOUSEINPUT(dx=nx, dy=ny, mouseData=data, dwFlags=flags, time=0, dwExtraInfo=0)
    return ev


_MOVE_FLAGS = MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK


def cursor_position() -> tuple[int, int] | None:
    """Where the pointer actually is, in physical pixels. None if unreadable.

    Only meaningful under a per-monitor DPI context; the executor verifies that
    on its own thread before injecting anything.
    """
    pt = wt.POINT()
    if not _user32.GetCursorPos(ctypes.byref(pt)):
        return None
    return int(pt.x), int(pt.y)


def move_events(x: int, y: int, desktop: VirtualDesktop | None = None) -> list[INPUT]:
    """Absolute move to (x, y) that is guaranteed to reach the target as an event.

    Windows emits nothing at all for a move to the pixel the cursor already
    occupies. Measured rather than assumed: a Tk window handed an absolute
    SendInput move to its own current cursor position receives only the press
    that follows -- no motion event whatsoever.

    That is how a click can be injected perfectly and still do nothing. An
    application that learns the pointer position from move events is pressed at
    a point it was never told about, and ignores it. Nothing in this process
    can tell: SendInput returns a full count, the executor records the action as
    executed, and the target's own screen is the only thing that disagrees.

    So when the cursor is already there, step one pixel aside first. Both events
    go in a single SendInput call, so no application can observe the cursor
    resting on the neighbour, and the neighbour is chosen inside the virtual
    desktop so normalization cannot reject it.
    """
    vd = desktop or virtual_desktop()
    nx, ny = normalize_absolute(x, y, vd)
    target = _mouse_event(_MOVE_FLAGS, nx, ny)

    here = cursor_position()
    # One pixel counts as "already there": normalization rounds, so a
    # neighbouring pixel can map to the same 0..65535 pair and be suppressed
    # for exactly the same reason.
    if here is None or abs(here[0] - x) > 1 or abs(here[1] - y) > 1:
        return [target]

    for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1)):
        if vd.contains(x + dx, y + dy):
            jx, jy = normalize_absolute(x + dx, y + dy, vd)
            if (jx, jy) != (nx, ny):
                return [_mouse_event(_MOVE_FLAGS, jx, jy), target]
    return [target]


def _key_event(vk: int, up: bool) -> INPUT:
    """Build a scancode key event.

    Scancodes rather than virtual keys: many games read DirectInput/RawInput and
    ignore virtual-key-only events entirely.
    """
    scan = _user32.MapVirtualKeyW(vk, MAPVK_VK_TO_VSC)
    flags = KEYEVENTF_SCANCODE | (KEYEVENTF_KEYUP if up else 0)
    if vk in _EXTENDED_VKS:
        flags |= KEYEVENTF_EXTENDEDKEY
    ev = INPUT(type=INPUT_KEYBOARD)
    ev.ki = KEYBDINPUT(wVk=0, wScan=scan, dwFlags=flags, time=0, dwExtraInfo=0)
    return ev


class Button(Enum):
    LEFT = (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP)
    RIGHT = (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP)
    MIDDLE = (MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP)


# --- action steps ---------------------------------------------------------


@dataclass(frozen=True)
class MoveTo:
    x: int
    y: int


@dataclass(frozen=True)
class ButtonDown:
    button: Button = Button.LEFT


@dataclass(frozen=True)
class ButtonUp:
    button: Button = Button.LEFT


@dataclass(frozen=True)
class KeyDown:
    vk: int


@dataclass(frozen=True)
class KeyUp:
    vk: int


@dataclass(frozen=True)
class Dwell:
    seconds: float


Step = MoveTo | ButtonDown | ButtonUp | KeyDown | KeyUp | Dwell

# Steps that introduce *new* input. Every one of these is authorized inside the
# dispatch boundary -- movement included, because a live cursor jump is itself an
# intrusion on whatever the operator is doing.
_NEW_INPUT_STEPS = (MoveTo, ButtonDown, KeyDown)

# Steps that undo input we are already holding. These must be able to proceed
# when the guard is denying everything, or a killed session leaves keys down.
_RELEASE_STEPS = (ButtonUp, KeyUp)


@dataclass(frozen=True)
class Outcome:
    """What actually became of a sequence, reported once it is finally decided.

    Acceptance is not execution. Between the two sit a queue, at least one
    dwell, and every interlock re-evaluated at press time, any of which can
    refuse. A caller told only that its action was accepted has been told
    something true about the queue and nothing about the game.
    """

    status: str              # sent | dry | denied | cancelled | error
    detail: str = ""
    action_id: int | None = None
    label: str = ""

    @property
    def reached_the_target(self) -> bool:
        return self.status == "sent"


@dataclass
class Sequence:
    steps: list[Step]
    label: str = ""
    # Optional provenance, carried through for the arbiter and the action log.
    meta: dict = field(default_factory=dict)
    # Called exactly once with an Outcome when this sequence is finally
    # decided -- executed, denied, cancelled or failed. Never called more than
    # once, and never left uncalled for a sequence that entered the queue.
    on_outcome: object | None = None
    # Called inside the dispatch boundary immediately before each new input.
    # Raising aborts the rest of the sequence. This is how an action stays tied
    # to the world it was reasoned about: validating once at submission leaves a
    # queue delay and a dwell during which the window can move, the capture
    # backend can be replaced, or a reflex can preempt -- and the press would
    # still land.
    validate: object | None = None


class InputExecutor:
    """Serializes every injection and can be interrupted mid-sequence.

    The move -> settle -> click pattern leaves a gap of tens of milliseconds
    between authorization and the actual button-down. Checking safety only at the
    start of a sequence means a kill or a focus change landing inside that gap is
    followed by the click anyway. So the guard runs immediately before *each*
    press.

    The converse matters just as much: once a key is physically down, refusing
    all further events after a kill would leave it held down in the game. Every
    successful press is tracked, and cancellation releases exactly those -- a
    path that can only ever release, never press.
    """

    def __init__(
        self,
        safety: SafetySupervisor,
        *,
        move_settle: float = 0.12,
        press_hold: float = 0.06,
    ) -> None:
        """
        ``move_settle`` is the pause between arriving at a point and pressing;
        ``press_hold`` is how long the button stays down.

        The first defaults are deliberately unhurried. A 16ms settle was the
        original value and it silently did nothing on a real UI: the cursor
        arrived, the app registered the hover, and the click was gone again
        before the button's hit test caught up. Nothing errored -- the click
        simply had no effect, which is the most expensive kind of failure to
        diagnose. 120ms is still far faster than a person and costs nothing next
        to model inference.
        """
        self._safety = safety
        self._move_settle = move_settle
        self._press_hold = press_hold
        self._queue: queue.Queue[Sequence | None] = queue.Queue()
        self._pressed_buttons: set[Button] = set()
        self._pressed_keys: set[int] = set()
        self._lock = threading.Lock()
        self._cancel = threading.Event()
        self._thread: threading.Thread | None = None
        # Deliberately NOT cached here. Virtual-desktop metrics are read on the
        # injecting thread, under a verified DPI context, immediately before each
        # normalization -- see _desktop_now().
        self.unreleased: set[str] = set()
        self._dpi_error: str | None = None
        self.executed = 0
        self.denied = 0
        self.cancelled = 0
        safety.on_kill(self._on_kill)

    # --- lifecycle ----------------------------------------------------

    def start(self) -> None:
        if self._thread:
            return
        # Fail fast for the common case; _run re-verifies on its own thread,
        # which is the check that actually governs injection.
        require_trustworthy_dpi()
        self._thread = threading.Thread(target=self._run, name="gamelens-input", daemon=True)
        self._thread.start()

    def shutdown(self) -> None:
        self._cancel.set()
        self._queue.put(None)
        t = self._thread
        if t:
            t.join(timeout=2.0)
        self._thread = None
        # Anything still queued when the thread stopped never ran, and a caller
        # waiting on its outcome would otherwise wait forever.
        self._drain("shutdown")
        self.release_all("shutdown")

    def submit(self, seq: Sequence) -> None:
        self._queue.put(seq)

    def _on_kill(self, reason: str) -> None:
        self._cancel.set()
        self._drain(f"kill: {reason}")
        self.release_all(f"kill: {reason}")

    def _drain(self, reason: str) -> None:
        try:
            while True:
                seq = self._queue.get_nowait()
                if seq is None:
                    continue
                self.cancelled += 1
                self._report(seq, "cancelled", reason)
        except queue.Empty:
            pass

    def _report(self, seq: Sequence, status: str, detail: str = "") -> None:
        """Deliver a sequence's final outcome. Never lets a callback kill the thread."""
        callback = seq.on_outcome
        if callback is None:
            return
        try:
            callback(Outcome(
                status=status,
                detail=detail,
                action_id=seq.meta.get("action_id"),
                label=seq.label,
            ))
        except Exception:
            log.exception("outcome callback for %r raised", seq.label)

    # --- the release path ----------------------------------------------

    def _release_one(self, kind: str, what) -> bool:
        """Release one tracked input. Returns True only if the OS accepted it.

        Tracking is updated *after* a confirmed send, never before. Clearing the
        set first means a rejected release permanently forgets an input that is
        still physically held: later cleanups see nothing to do and the snapshot
        reports all clear while the game still has the key down.

        Callers must already hold the release boundary, which is what keeps a
        normal key-up and a cleanup release from both firing for the same key.
        """
        with self._lock:
            tracked = self._pressed_buttons if kind == "button" else self._pressed_keys
            if what not in tracked:
                return True          # someone else released it; nothing owed

        event = (_mouse_event(what.value[1]) if kind == "button"
                 else _key_event(what, up=True))
        label = f"{kind}:{what.name if kind == 'button' else what}"
        try:
            _send([event])
        except (InjectionFailed, OSError):
            self.unreleased.add(label)
            log.exception("release of %s failed; it may still be held", label)
            return False

        with self._lock:
            (self._pressed_buttons if kind == "button" else self._pressed_keys).discard(what)
        self.unreleased.discard(label)
        return True

    def release_all(self, reason: str) -> None:
        """Release everything we are still holding. Never presses anything.

        Deliberately carries no interlocks: after a kill the guard denies
        everything, and denying the key-up too is exactly how a game ends up with
        W held down forever. The path is bounded by the tracked set, so it cannot
        synthesise a new press. Released one at a time so a partial failure only
        loses the ones that actually failed.
        """
        with self._safety.release_boundary():
            with self._lock:
                buttons, keys = set(self._pressed_buttons), set(self._pressed_keys)
            if not buttons and not keys:
                return

            ok = sum(self._release_one("button", b) for b in buttons)
            ok += sum(self._release_one("key", k) for k in keys)
            total = len(buttons) + len(keys)
            if ok == total:
                log.warning("released %d input(s) after %s", total, reason)
            else:
                log.error(
                    "released %d of %d input(s) after %s; still held: %s",
                    ok, total, reason, sorted(self.unreleased),
                )

    def _release_step(self, step: ButtonUp | KeyUp, label: str) -> None:
        with self._safety.release_boundary():
            if self._safety.dry_run:
                log.info("[dry-run] %s %s", label, step)
                return
            if isinstance(step, ButtonUp):
                self._release_one("button", step.button)
            else:
                self._release_one("key", step.vk)

    # --- execution ------------------------------------------------------

    def _desktop_now(self) -> VirtualDesktop:
        """Virtual-desktop metrics, read fresh on the injecting thread.

        Caching these for the executor's lifetime is wrong: unplug a monitor on
        the left and the real rectangle changes from (-1920, 3840 wide) to
        (0, 1920 wide), so a cached rectangle normalizes a click on the primary
        monitor to roughly half a screen off. The target window need not move, so
        geometry checks do not necessarily catch it.
        """
        return virtual_desktop()

    def _run(self) -> None:
        # The DPI context is per-thread, so verifying it in start() proves
        # something about the *caller*, not about the thread that injects. This
        # is that thread.
        try:
            require_trustworthy_dpi()
        except RuntimeError as exc:
            log.error("input executor refusing to run: %s", exc)
            self._dpi_error = str(exc)
            return

        while True:
            seq = self._queue.get()
            if seq is None:
                return
            if self._cancel.is_set():
                self.cancelled += 1
                self._report(seq, "cancelled", "executor is cancelled")
                continue
            try:
                status = self._execute(seq)
                self.executed += 1
                self._report(seq, status)
            except NotPermitted as exc:
                self.denied += 1
                log.warning("sequence %r denied: %s", seq.label, exc)
                self._report(seq, "denied", exc.reason.value)
                # A denial mid-sequence can leave something pressed.
                self.release_all(f"denied: {exc.reason.value}")
            except Exception as exc:
                self.denied += 1
                # The arbiter's press-time revalidation raises its own type.
                # Duck-typed rather than imported: arbiter imports this module,
                # and the reason is the only part worth reporting anyway.
                reason = getattr(getattr(exc, "reason", None), "value", None)
                if reason:
                    log.warning("sequence %r refused at press time: %s", seq.label, exc)
                    self._report(seq, "denied", reason)
                else:
                    log.exception("sequence %r failed", seq.label)
                    self._report(seq, "error", str(exc))
                self.release_all("execution error")

    def _execute(self, seq: Sequence) -> str:
        """Run a sequence. Returns "sent" if anything was really injected.

        The distinction matters to whoever is watching: in dry-run every step
        is authorized and logged and nothing reaches the game, which looks
        identical from the queue's point of view and not at all identical from
        the operator's.
        """
        injected = False
        for step in seq.steps:
            if self._cancel.is_set():
                raise NotPermitted(Denial.KILLED, "cancelled mid-sequence")

            if isinstance(step, Dwell):
                # Outside every boundary: it is the one slow step, and holding a
                # lock across it would block kill() for its whole duration.
                # Interruptible, so a kill during the dwell is not followed by
                # the click it was waiting to make.
                if self._cancel.wait(step.seconds):
                    raise NotPermitted(Denial.KILLED, "killed during dwell")
                continue

            if isinstance(step, _RELEASE_STEPS):
                self._release_step(step, seq.label)
                continue

            # Every *new* injected action -- movement included. Moving the cursor
            # is not harmless: while live it drags the pointer across whatever
            # the operator is actually doing, so it belongs behind the same
            # foreground, watchdog and rate-limit interlocks as a click, and
            # behind the same boundary so a kill cannot land between the
            # approval and the event.
            with self._safety.commit():
                if seq.validate is not None:
                    # Inside the boundary: whatever this checks stays true until
                    # the press below has happened.
                    seq.validate()
                if self._safety.dry_run:
                    log.info("[dry-run] %s %s", seq.label, step)
                else:
                    self._apply_new(step)
                    injected = True

        return "sent" if injected else "dry"

    def _apply_new(self, step: Step) -> None:
        """Inject one new input. Tracking is recorded only after a confirmed send."""
        if isinstance(step, MoveTo):
            _send(move_events(step.x, step.y, self._desktop_now()))
        elif isinstance(step, ButtonDown):
            _send([_mouse_event(step.button.value[0])])
            with self._lock:
                self._pressed_buttons.add(step.button)
        elif isinstance(step, KeyDown):
            _send([_key_event(step.vk, up=False)])
            with self._lock:
                self._pressed_keys.add(step.vk)
        else:  # pragma: no cover - guarded by _execute's dispatch
            raise TypeError(f"{step!r} is not a new-input step")

    # --- convenience builders -------------------------------------------

    def click_at_screen(
        self, x: int, y: int, button: Button = Button.LEFT, label: str = "click"
    ) -> Sequence:
        return Sequence(
            steps=[MoveTo(x, y), Dwell(self._move_settle), ButtonDown(button),
                   Dwell(self._press_hold), ButtonUp(button)],
            label=label,
        )

    def click_in_frame(
        self, geom: Geometry, fx: float, fy: float, frame_w: int, frame_h: int,
        button: Button = Button.LEFT, label: str = "click",
    ) -> Sequence:
        sx, sy = geom.frame_to_screen(fx, fy, frame_w, frame_h)
        seq = self.click_at_screen(sx, sy, button, label)
        seq.meta.update(
            frame_xy=(fx, fy), screen_xy=(sx, sy),
            geometry_generation=geom.generation, hwnd=geom.hwnd,
        )
        return seq

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "executed": self.executed,
                "denied": self.denied,
                "cancelled": self.cancelled,
                "pressed_buttons": [b.name for b in self._pressed_buttons],
                "pressed_keys": sorted(self._pressed_keys),
                "unreleased": sorted(self.unreleased),
                "dpi_error": self._dpi_error,
                "queue_depth": self._queue.qsize(),
            }
