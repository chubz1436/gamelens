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
MOUSEEVENTF_XDOWN = 0x0080
MOUSEEVENTF_XUP = 0x0100
MOUSEEVENTF_WHEEL = 0x0800
MOUSEEVENTF_HWHEEL = 0x01000
WHEEL_DELTA = 120
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
_user32.WindowFromPoint.argtypes = [wt.POINT]
_user32.WindowFromPoint.restype = wt.HWND
_user32.GetAncestor.argtypes = [wt.HWND, wt.UINT]
_user32.GetAncestor.restype = wt.HWND
_user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
_user32.GetAsyncKeyState.restype = ctypes.c_short
GA_ROOT = 2


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


def pointer_on_window(hwnd: int) -> bool:
    """Is the cursor over ``hwnd`` (or one of its children) right now?

    A press or a wheel notch with no coordinate of its own lands wherever the
    cursor is. In a game that locks the cursor that is the game; in a windowed
    one a relative `look` can walk it off the edge, and the foreground check
    still passes until the click has already landed on whatever is there
    (GL040-I01). Unreadable counts as no.
    """
    here = cursor_position()
    if here is None:
        return False
    child = _user32.WindowFromPoint(wt.POINT(*here))
    root = _user32.GetAncestor(child, GA_ROOT) if child else None
    return bool(root) and int(root) == int(hwnd)


# Keys that turn a game key into a shell chord: either Alt (Alt+F4, Alt+Tab)
# and either Windows key. Ctrl too, but only in front of escape (Ctrl+Esc,
# Ctrl+Shift+Esc). Checked by physical state, because the Owner holding one
# is as good as GameLens pressing it (GL040-I03).
CHORD_MODIFIERS = (0xA4, 0xA5, 0x5B, 0x5C)          # LAlt RAlt LWin RWin
ESCAPE_MODIFIERS = (0xA2, 0xA3)                     # LCtrl RCtrl
VK_ESCAPE = 0x1B


def held_keys(vks) -> set[int]:
    """Which of ``vks`` are down right now, by the async (physical+injected) state."""
    return {vk for vk in vks if _user32.GetAsyncKeyState(vk) & 0x8000}


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
    go in a single SendInput call, which is documented not to interleave any
    other thread's input between them -- the target sees two moves back to back
    and never a pointer parked on the neighbour with something else in front of
    it. The neighbour is chosen inside the virtual desktop so normalization
    cannot reject it, and skipped if it rounds to the same normalized pair,
    because that would be suppressed for the very same reason.
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
    """(down flags, up flags, mouseData). The side buttons share their flags and
    differ only in mouseData, which is also what keeps the two values distinct."""

    LEFT = (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP, 0)
    RIGHT = (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP, 0)
    MIDDLE = (MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP, 0)
    X1 = (MOUSEEVENTF_XDOWN, MOUSEEVENTF_XUP, 1)      # "mouse 4", back
    X2 = (MOUSEEVENTF_XDOWN, MOUSEEVENTF_XUP, 2)      # "mouse 5", forward

    def event(self, up: bool) -> INPUT:
        return _mouse_event(self.value[1] if up else self.value[0], data=self.value[2])


# Every name a caller may use for a mouse button. A typo is refused, never read
# as a left click.
BUTTON_NAMES: dict[str, Button] = {
    "left": Button.LEFT, "right": Button.RIGHT, "middle": Button.MIDDLE,
    "x1": Button.X1, "mouse4": Button.X1, "x2": Button.X2, "mouse5": Button.X2,
}


def button_from_name(name) -> Button:
    """Resolve a caller's button name, or raise ValueError."""
    button = BUTTON_NAMES.get(str(name).strip().lower())
    if button is None:
        raise ValueError(
            f"unknown button {name!r}; use {', '.join(BUTTON_NAMES)}")
    return button


# Names a caller may use instead of a virtual-key code. An allowlist, not a
# convenience: a caller that can name any VK can send Alt+F4, Ctrl+Alt+Del's
# reachable parts, or the Windows key, none of which are "input to the game".
# Anything outside this table is refused rather than translated.
#
# Wide enough for any game's default bindings, not only Minecraft's: every
# F-key a game binds, the punctuation row, the navigation block and the numpad.
# Still out: the Windows and menu keys (the shell's, not the game's), F12 and
# Pause (GameLens's own kill switch -- an agent must not be able to press it,
# or to be the reason it looks pressed), PrintScreen, and the lock keys, which
# change the Owner's keyboard state after GameLens has gone.
KEY_NAMES: dict[str, int] = {
    **{c: ord(c.upper()) for c in "abcdefghijklmnopqrstuvwxyz"},
    **{str(d): ord(str(d)) for d in range(10)},
    **{f"f{n}": 0x6F + n for n in range(1, 12)},                 # F1..F11
    **{f"numpad{d}": 0x60 + d for d in range(10)},
    "multiply": 0x6A, "add": 0x6B, "subtract": 0x6D, "decimal": 0x6E, "divide": 0x6F,
    "space": 0x20, "enter": 0x0D, "tab": 0x09, "escape": 0x1B, "esc": 0x1B,
    "backspace": 0x08,
    "shift": 0xA0, "ctrl": 0xA2, "alt": 0xA4,
    "rshift": 0xA1, "rctrl": 0xA3, "ralt": 0xA5,
    "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
    "insert": 0x2D, "delete": 0x2E, "home": 0x24, "end": 0x23,
    "pageup": 0x21, "pagedown": 0x22,
    # US-layout names for the punctuation keys; the key is the physical one,
    # whatever it prints on another layout.
    "minus": 0xBD, "equals": 0xBB, "lbracket": 0xDB, "rbracket": 0xDD,
    "backslash": 0xDC, "semicolon": 0xBA, "quote": 0xDE, "comma": 0xBC,
    "period": 0xBE, "slash": 0xBF, "backtick": 0xC0,
    "-": 0xBD, "=": 0xBB, "[": 0xDB, "]": 0xDD, "\\": 0xDC, ";": 0xBA,
    "'": 0xDE, ",": 0xBC, ".": 0xBE, "/": 0xBF, "`": 0xC0,
}


def key_code(name: str) -> int:
    """Resolve a key name to a virtual-key code, or refuse."""
    vk = KEY_NAMES.get(str(name).strip().lower())
    if vk is None:
        raise ValueError(
            f"unknown key {name!r}; allowed: {', '.join(sorted(KEY_NAMES))}"
        )
    return vk


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
class LookBy:
    """A *relative* mouse move, in mickeys, for a game that has grabbed the cursor.

    Absolute positioning is useless once a game captures the pointer: it hides
    the cursor, re-centres it every frame and reads the delta, so a move to an
    absolute screen point produces whatever delta happens to fall out of the
    difference. Turning a camera means saying how far to turn.

    The deltas pass through the pointer-speed and acceleration settings, so the
    same numbers do not mean the same angle on two machines. That is a property
    of the mechanism, not something to correct for here.
    """

    dx: int
    dy: int


@dataclass(frozen=True)
class Scroll:
    """Turn the wheel by whole notches. Negative ``clicks`` is down (or left).

    Wheel input is relative and has no position of its own: it goes wherever
    the cursor already is, which in a pointer-locked game is the crosshair.
    """

    clicks: int
    horizontal: bool = False


@dataclass(frozen=True)
class Dwell:
    seconds: float


Step = MoveTo | LookBy | Scroll | ButtonDown | ButtonUp | KeyDown | KeyUp | Dwell

# Steps that introduce *new* input. Every one of these is authorized inside the
# dispatch boundary -- movement included, because a live cursor jump is itself an
# intrusion on whatever the operator is doing.
_NEW_INPUT_STEPS = (MoveTo, LookBy, Scroll, ButtonDown, KeyDown)

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
    completed_steps: int = 0
    injected_steps: int = 0
    last_completed_step: int | None = None
    partial: bool = False

    @property
    def reached_the_target(self) -> bool:
        return self.status == "sent" or self.injected_steps > 0


@dataclass
class Sequence:
    steps: list[Step]
    label: str = ""
    completed_steps: int = field(default=0, init=False)
    injected_steps: int = field(default=0, init=False)
    last_completed_step: int | None = field(default=None, init=False)
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
        queue_capacity: int = 32,
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
        if isinstance(queue_capacity, bool) or not isinstance(queue_capacity, int) or queue_capacity < 1:
            raise ValueError("queue_capacity must be a positive integer")
        self._queue: queue.Queue[Sequence | None] = queue.Queue(maxsize=queue_capacity)
        self._admission = threading.Lock()
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
        with self._admission:
            self._cancel.set()
        self._drain("shutdown")
        self._queue.put_nowait(None)
        t = self._thread
        if t:
            t.join(timeout=2.0)
        self._thread = None
        # Anything still queued when the thread stopped never ran, and a caller
        # waiting on its outcome would otherwise wait forever.
        self._drain("shutdown")
        self.release_all("shutdown")

    def submit(self, seq: Sequence) -> bool:
        # Never wait behind a full queue; callers need an immediate honest result.
        with self._admission:
            if self._cancel.is_set():
                reason = "executor is cancelled"
            else:
                try:
                    self._queue.put_nowait(seq)
                    return True
                except queue.Full:
                    reason = "input queue is full; nothing was queued"
        self.cancelled += 1
        self._report(seq, "cancelled", reason)
        return False

    def _on_kill(self, reason: str) -> None:
        with self._admission:
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
        partial = seq.injected_steps > 0 and status != "sent"
        if partial:
            detail = f"PARTIAL INPUT: {seq.injected_steps} input steps were sent; do not replay the sequence. {detail}"
        try:
            callback(Outcome(
                status=status,
                detail=detail,
                completed_steps=seq.completed_steps,
                injected_steps=seq.injected_steps,
                last_completed_step=seq.last_completed_step,
                partial=partial,
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

        event = (what.event(up=True) if kind == "button"
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
            ok = (self._release_one("button", step.button) if isinstance(step, ButtonUp)
                  else self._release_one("key", step.vk))
        if not ok:
            # Carrying on would report "sent" with the key still down, and the
            # next action would press on top of it -- a stuck alt followed by
            # an F4 is Alt+F4 (GL040-I02). Raising sends the sequence to the
            # executor's error path, which releases everything and reports it.
            raise InjectionFailed(f"release of {step} failed; it may still be held")

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
        expected_pointer = None
        if self.unreleased:
            # One more attempt before refusing: the failure may have been
            # transient. Outside every boundary, as release_all requires.
            self.release_all("retrying a failed release before new input")
        seq.completed_steps = seq.injected_steps = 0
        seq.last_completed_step = None
        for index, step in enumerate(seq.steps):
            if self._cancel.is_set():
                raise NotPermitted(Denial.KILLED, "cancelled mid-sequence")

            if isinstance(step, Dwell):
                # Outside every boundary: it is the one slow step, and holding a
                # lock across it would block kill() for its whole duration.
                # Interruptible, so a kill during the dwell is not followed by
                # the click it was waiting to make.
                if self._cancel.wait(step.seconds):
                    raise NotPermitted(Denial.KILLED, "killed during dwell")
                seq.completed_steps = index + 1
                seq.last_completed_step = index
                continue

            if isinstance(step, _RELEASE_STEPS):
                self._release_step(step, seq.label)
                seq.completed_steps = index + 1
                seq.last_completed_step = index
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
                    self._guard_input(step)
                    if isinstance(step, ButtonDown) and expected_pointer is not None:
                        here = cursor_position()
                        if here is None or max(abs(here[i]-expected_pointer[i]) for i in (0, 1)) > 2:
                            raise NotPermitted(Denial.POINTER_MOVED)
                    self._apply_new(step)
                    seq.injected_steps += 1
                    if isinstance(step, MoveTo):
                        expected_pointer = (step.x, step.y)
                    elif isinstance(step, LookBy):
                        expected_pointer = None
                    injected = True
                seq.completed_steps = index + 1
                seq.last_completed_step = index

        return "sent" if injected else "dry"

    def _guard_input(self, step: Step) -> None:
        """The checks that depend on the input itself, at the moment it is sent.

        Inside the commit boundary, after every interlock, immediately before
        the event. Raises NotPermitted, which the executor reports as denied
        and follows with release_all.
        """
        if self.unreleased:
            raise NotPermitted(Denial.UNRELEASED_INPUT, ", ".join(sorted(self.unreleased)))
        if isinstance(step, KeyDown):
            watch = CHORD_MODIFIERS + (ESCAPE_MODIFIERS if step.vk == VK_ESCAPE else ())
            with self._lock:
                ours = set(self._pressed_keys)
            foreign = held_keys(watch) - ours
            if foreign:
                raise NotPermitted(Denial.FOREIGN_MODIFIER,
                                   "vk " + ", ".join(f"{vk:#04x}" for vk in sorted(foreign)))
        elif isinstance(step, (ButtonDown, Scroll)):
            if not pointer_on_window(self._safety.target_hwnd):
                raise NotPermitted(Denial.POINTER_OFF_TARGET, str(cursor_position()))

    def _apply_new(self, step: Step) -> None:
        """Inject one new input. Tracking is recorded only after a confirmed send."""
        if isinstance(step, MoveTo):
            _send(move_events(step.x, step.y, self._desktop_now()))
        elif isinstance(step, LookBy):
            # No ABSOLUTE flag: this is a delta, and it is the only kind of
            # mouse input a pointer-locked game will interpret as a turn.
            _send([_mouse_event(MOUSEEVENTF_MOVE, step.dx, step.dy)])
        elif isinstance(step, Scroll):
            # mouseData is a DWORD carrying a signed count; a negative one has
            # to be handed over as its two's complement.
            data = (step.clicks * WHEEL_DELTA) & 0xFFFFFFFF
            flag = MOUSEEVENTF_HWHEEL if step.horizontal else MOUSEEVENTF_WHEEL
            _send([_mouse_event(flag, data=data)])
        elif isinstance(step, ButtonDown):
            _send([step.button.event(up=False)])
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
                "queue_capacity": self._queue.maxsize,
            }
