"""Process-wide DPI awareness, established and then *verified*.

This must run before any other Win32 window call in the process. If it does not,
Windows silently hands back virtualized (pre-scaled) rectangles on any display
that is not at 100% scaling. Nothing errors -- every coordinate is quietly wrong
by the scale factor, surfacing much later as clicks landing next to their target.

Setting awareness is not enough to trust it. A host process can already be
DPI-unaware or system-aware (via its manifest, or because an embedding app set it
first), and in that case the setters below all fail and the process keeps the
*old* mode. Assuming success there is a fail-open: we would go on labelling
virtualized rects as physical. So ``ensure_dpi_aware`` reads the effective
awareness back and reports whether coordinates can be trusted. Callers that map
or inject coordinates must refuse to operate when they cannot.
"""

from __future__ import annotations

import ctypes
import logging
import sys
from dataclasses import dataclass

log = logging.getLogger(__name__)

# DPI_AWARENESS_CONTEXT values (winuser.h). These are sentinel HANDLE values,
# not small integers, so they must be passed pointer-sized.
_CTX_PER_MONITOR_AWARE_V2 = ctypes.c_void_p(-4)
_CTX_PER_MONITOR_AWARE = ctypes.c_void_p(-3)

# DPI_AWARENESS enum.
AWARENESS_INVALID = -1
AWARENESS_UNAWARE = 0
AWARENESS_SYSTEM = 1
AWARENESS_PER_MONITOR = 2

_AWARENESS_NAMES = {
    AWARENESS_INVALID: "invalid",
    AWARENESS_UNAWARE: "unaware",
    AWARENESS_SYSTEM: "system-aware",
    AWARENESS_PER_MONITOR: "per-monitor-aware",
}


@dataclass(frozen=True)
class DpiState:
    """What we asked for, what we actually got, and whether it is usable."""

    requested: str
    effective: str
    awareness: int
    trustworthy: bool
    reason: str

    def __str__(self) -> str:
        verdict = "coordinates trustworthy" if self.trustworthy else f"UNUSABLE: {self.reason}"
        return f"{self.effective} (requested {self.requested}) -- {verdict}"


_state: DpiState | None = None
_requested: str = "unset"


def _effective_awareness() -> int:
    """Read the awareness actually in force for this thread, not what we asked for."""
    user32 = ctypes.windll.user32
    try:
        get_ctx = user32.GetThreadDpiAwarenessContext
        # The return is a DPI_AWARENESS_CONTEXT handle. Without an explicit
        # restype ctypes truncates it to a 32-bit int on 64-bit Python and the
        # lookup below returns garbage.
        get_ctx.restype = ctypes.c_void_p
        get_ctx.argtypes = []

        from_ctx = user32.GetAwarenessFromDpiAwarenessContext
        from_ctx.restype = ctypes.c_int
        from_ctx.argtypes = [ctypes.c_void_p]

        return from_ctx(get_ctx())
    except (AttributeError, OSError):
        pass

    # Win8.1 path: process-level query.
    try:
        value = ctypes.c_int()
        if ctypes.windll.shcore.GetProcessDpiAwareness(None, ctypes.byref(value)) == 0:
            return value.value
    except (AttributeError, OSError):
        pass

    return AWARENESS_INVALID


def _try_set() -> str:
    """Attempt to raise this process to per-monitor awareness. Returns what was tried."""
    user32 = ctypes.windll.user32

    # Win10 1703+. Preferred: correct rects on mixed-DPI multi-monitor setups.
    try:
        if user32.SetProcessDpiAwarenessContext(_CTX_PER_MONITOR_AWARE_V2):
            return "per-monitor-v2"
        if user32.SetProcessDpiAwarenessContext(_CTX_PER_MONITOR_AWARE):
            return "per-monitor-v1"
    except (AttributeError, OSError):
        pass

    # Win8.1 fallback. 2 == PROCESS_PER_MONITOR_DPI_AWARE.
    try:
        if ctypes.windll.shcore.SetProcessDpiAwareness(2) == 0:
            return "shcore-per-monitor"
    except (AttributeError, OSError):
        pass

    # Vista+ system-wide. Still wrong on mixed DPI, but better than unaware.
    try:
        if user32.SetProcessDPIAware():
            return "system-aware"
    except (AttributeError, OSError):
        pass

    return "none-accepted"


def _classify(awareness: int, requested: str) -> DpiState:
    effective = _AWARENESS_NAMES.get(awareness, f"unknown({awareness})")
    if awareness == AWARENESS_PER_MONITOR:
        return DpiState(requested, effective, awareness, True, "")
    if awareness == AWARENESS_SYSTEM:
        reason = (
            "only system-DPI-aware, so window rects are virtualized on any monitor whose "
            "scaling differs from the primary; mapped coordinates would be silently wrong there"
        )
    elif awareness == AWARENESS_UNAWARE:
        reason = (
            "DPI-unaware -- every rect is virtualized to 96 DPI and no coordinate can be "
            "mapped to a real screen pixel"
        )
    else:
        reason = "could not determine the effective DPI awareness"
    return DpiState(requested, effective, awareness, False, reason)


def ensure_dpi_aware() -> DpiState:
    """Raise this process to per-monitor DPI awareness once, and report what took.

    The *attempt* is process-wide and worth caching. The resulting state is not a
    safe thing to cache for later use, because DPI awareness is a **per-thread**
    context: a worker thread can hold a different (and lower) awareness than the
    thread that imported this module. Use require_trustworthy_dpi() on the thread
    that is about to read geometry or inject, not this cached value.
    """
    global _state, _requested
    if _state is not None:
        return _state

    if not sys.platform.startswith("win"):
        _requested = "n/a"
        _state = DpiState("n/a", "n/a", AWARENESS_INVALID, False, "not running on Windows")
        return _state

    _requested = _try_set()
    _state = _classify(_effective_awareness(), _requested)

    if _state.trustworthy:
        log.debug("DPI: %s", _state)
    else:
        log.error("DPI: %s", _state)
    return _state


def thread_dpi_state() -> DpiState:
    """Effective DPI awareness of the **calling thread**, measured now.

    Never cached. Thread contexts differ and can change independently of the
    process default, so a value measured on one thread says nothing about another.
    """
    if not sys.platform.startswith("win"):
        return DpiState("n/a", "n/a", AWARENESS_INVALID, False, "not running on Windows")
    ensure_dpi_aware()
    return _classify(_effective_awareness(), _requested)


def require_trustworthy_dpi() -> None:
    """Raise unless *this thread* can map coordinates correctly.

    Measured per call, on the calling thread. Checking a value cached at import
    time would let a worker thread running under a lower awareness sail past this
    guard and read virtualized geometry.
    """
    state = thread_dpi_state()
    if not state.trustworthy:
        raise RuntimeError(
            f"GameLens refuses to map or inject coordinates: {state.reason}. "
            f"Effective awareness is {state.effective!r}. Run GameLens as its own "
            f"process (not embedded in a DPI-unaware host) so it can set per-monitor "
            f"awareness before any window call, and make sure the calling thread has "
            f"not been given a lower DPI context."
        )


def scale_for_window(hwnd: int) -> float:
    """DPI scale factor (1.0 == 96 DPI) of the monitor showing *hwnd*."""
    try:
        dpi = ctypes.windll.user32.GetDpiForWindow(hwnd)
        if dpi:
            return dpi / 96.0
    except (AttributeError, OSError):
        pass
    return 1.0
