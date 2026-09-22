"""Frame <-> client <-> screen coordinate mapping, with a generation counter.

Every mapping here is in *physical* pixels and is only valid under a verified
per-monitor DPI awareness context -- see gamelens/dpi.py. Callers must hold a
``Geometry`` snapshot and pass it explicitly rather than re-reading the window
mid-calculation, because a window that moves between two reads produces a mapping
that is internally inconsistent and silently wrong.

The frame basis is *measured, not assumed*. Depending on the capture backend and
the window's frame style, a captured frame corresponds either to the DWM extended
bounds or to the full ``GetWindowRect`` including the invisible resize border
(roughly 7px per side on Windows 10). Guessing wrong offsets every click by that
border. So we compare the actual captured frame size against both candidates and
refuse to map when neither matches.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import itertools
import threading
import time
from dataclasses import dataclass
from enum import Enum

import win32gui

from gamelens.dpi import require_trustworthy_dpi, scale_for_window
from gamelens.windows import extended_frame_bounds, is_alive

_user32 = ctypes.windll.user32

# GetSystemMetrics indices for the virtual desktop (all monitors combined).
_SM_XVIRTUALSCREEN = 76
_SM_YVIRTUALSCREEN = 77
_SM_CXVIRTUALSCREEN = 78
_SM_CYVIRTUALSCREEN = 79

_generation = itertools.count(1)


class FrameBasis(Enum):
    """Which screen rectangle a captured frame's (0,0) corresponds to."""

    DWM_BOUNDS = "dwm"          # DWM extended frame bounds (usual for WGC)
    WINDOW_RECT = "window"      # GetWindowRect, including the invisible border
    CLIENT = "client"           # client area only (PrintWindow with some flags)


class GeometryStale(RuntimeError):
    """The window moved, resized or died since this snapshot was taken."""


@dataclass(frozen=True)
class VirtualDesktop:
    left: int
    top: int
    width: int
    height: int

    @property
    def right(self) -> int:
        return self.left + self.width

    @property
    def bottom(self) -> int:
        return self.top + self.height

    def contains(self, x: int, y: int) -> bool:
        return self.left <= x < self.right and self.top <= y < self.bottom


def virtual_desktop() -> VirtualDesktop:
    """Bounding box of all monitors. The origin is negative when a monitor sits
    left of or above the primary -- which is exactly the case that breaks naive
    absolute-coordinate math."""
    g = _user32.GetSystemMetrics
    return VirtualDesktop(
        left=g(_SM_XVIRTUALSCREEN),
        top=g(_SM_YVIRTUALSCREEN),
        width=g(_SM_CXVIRTUALSCREEN),
        height=g(_SM_CYVIRTUALSCREEN),
    )


def _client_origin_on_screen(hwnd: int) -> tuple[int, int]:
    pt = wt.POINT(0, 0)
    if not _user32.ClientToScreen(wt.HWND(hwnd), ctypes.byref(pt)):
        raise GeometryStale(f"ClientToScreen failed for hwnd {hwnd}")
    return pt.x, pt.y


@dataclass(frozen=True)
class Geometry:
    """An immutable snapshot of one window's placement, in physical pixels."""

    generation: int
    hwnd: int
    captured_at: float

    dwm_left: int
    dwm_top: int
    dwm_width: int
    dwm_height: int

    win_left: int
    win_top: int
    win_width: int
    win_height: int

    client_left: int
    client_top: int
    client_width: int
    client_height: int

    dpi_scale: float

    # --- basis resolution -------------------------------------------------

    def basis_for(self, frame_width: int, frame_height: int) -> FrameBasis:
        """Work out which rectangle a frame of this size corresponds to.

        Raises if none matches: an unrecognised frame size means the window
        resized mid-capture or the backend is scaling, and mapping through a
        guessed basis would put every click in the wrong place.
        """
        candidates = (
            (FrameBasis.DWM_BOUNDS, self.dwm_width, self.dwm_height),
            (FrameBasis.WINDOW_RECT, self.win_width, self.win_height),
            (FrameBasis.CLIENT, self.client_width, self.client_height),
        )
        for basis, w, h in candidates:
            if w == frame_width and h == frame_height:
                return basis
        raise GeometryStale(
            f"frame {frame_width}x{frame_height} matches no known rect for hwnd "
            f"{self.hwnd} (dwm {self.dwm_width}x{self.dwm_height}, "
            f"window {self.win_width}x{self.win_height}, "
            f"client {self.client_width}x{self.client_height}); "
            f"refusing to map coordinates through a guessed basis"
        )

    def _origin(self, basis: FrameBasis) -> tuple[int, int]:
        if basis is FrameBasis.DWM_BOUNDS:
            return self.dwm_left, self.dwm_top
        if basis is FrameBasis.WINDOW_RECT:
            return self.win_left, self.win_top
        return self.client_left, self.client_top

    # --- mappings ---------------------------------------------------------

    def frame_to_screen(
        self, x: float, y: float, frame_width: int, frame_height: int
    ) -> tuple[int, int]:
        """Map a pixel in a captured frame to a physical screen pixel."""
        basis = self.basis_for(frame_width, frame_height)
        ox, oy = self._origin(basis)
        return int(round(ox + x)), int(round(oy + y))

    def screen_to_frame(
        self, x: float, y: float, frame_width: int, frame_height: int
    ) -> tuple[int, int]:
        basis = self.basis_for(frame_width, frame_height)
        ox, oy = self._origin(basis)
        return int(round(x - ox)), int(round(y - oy))

    def client_to_screen(self, x: float, y: float) -> tuple[int, int]:
        return int(round(self.client_left + x)), int(round(self.client_top + y))

    def screen_to_client(self, x: float, y: float) -> tuple[int, int]:
        return int(round(x - self.client_left)), int(round(y - self.client_top))

    def contains_screen_point(self, x: int, y: int) -> bool:
        """True when a screen point falls inside this window's visible frame."""
        return (
            self.dwm_left <= x < self.dwm_left + self.dwm_width
            and self.dwm_top <= y < self.dwm_top + self.dwm_height
        )

    # --- staleness --------------------------------------------------------

    def matches_current(self) -> bool:
        """Cheap re-read: has the window moved, resized or died since the snapshot?

        Compares *placement* only. Dataclass equality would also compare
        ``captured_at``, which is a fresh ``time.monotonic()`` on every snapshot,
        so an unchanged window would always compare unequal and every action
        validated through this predicate would be rejected as stale.
        """
        try:
            return _same_placement(self, snapshot(self.hwnd))
        except (GeometryStale, RuntimeError):
            return False

    def age_seconds(self) -> float:
        return time.monotonic() - self.captured_at


def snapshot(hwnd: int, _generation_value: int | None = None) -> Geometry:
    """Take a consistent placement snapshot of *hwnd*.

    Requires a trustworthy DPI context: without one the rects below are
    virtualized and mapping them to screen pixels produces confidently wrong
    answers rather than an error.
    """
    require_trustworthy_dpi()

    if not is_alive(hwnd):
        raise GeometryStale(f"hwnd {hwnd} is gone")

    dl, dt, dr, db = extended_frame_bounds(hwnd)
    wl, wt_, wr, wb = win32gui.GetWindowRect(hwnd)
    _, _, cw, ch = win32gui.GetClientRect(hwnd)
    cl, ct = _client_origin_on_screen(hwnd)

    return Geometry(
        generation=_generation_value if _generation_value is not None else next(_generation),
        hwnd=hwnd,
        captured_at=time.monotonic(),
        dwm_left=dl, dwm_top=dt, dwm_width=dr - dl, dwm_height=db - dt,
        win_left=wl, win_top=wt_, win_width=wr - wl, win_height=wb - wt_,
        client_left=cl, client_top=ct, client_width=cw, client_height=ch,
        dpi_scale=scale_for_window(hwnd),
    )


class GeometryTracker:
    """Holds the current snapshot and bumps the generation only on real change.

    The arbiter compares generations to decide whether an action computed against
    an older view of the window is still safe to execute, so the counter must
    advance on every move, resize or DPI change -- and must *not* advance when
    nothing moved, or every action would be rejected.
    """

    def __init__(self, hwnd: int) -> None:
        self._hwnd = hwnd
        self._lock = threading.Lock()
        self._current = snapshot(hwnd)

    @property
    def current(self) -> Geometry:
        with self._lock:
            return self._current

    @property
    def generation(self) -> int:
        with self._lock:
            return self._current.generation

    def refresh(self) -> Geometry:
        """Re-read placement. Returns the snapshot in force afterwards."""
        fresh = snapshot(self._hwnd)
        with self._lock:
            cur = self._current
            if _same_placement(cur, fresh):
                return cur                      # unchanged: keep the generation
            self._current = fresh               # changed: fresh already has a new one
            return fresh


def _same_placement(a: Geometry, b: Geometry) -> bool:
    return (
        a.hwnd == b.hwnd
        and (a.dwm_left, a.dwm_top, a.dwm_width, a.dwm_height)
        == (b.dwm_left, b.dwm_top, b.dwm_width, b.dwm_height)
        and (a.win_left, a.win_top, a.win_width, a.win_height)
        == (b.win_left, b.win_top, b.win_width, b.win_height)
        and (a.client_left, a.client_top, a.client_width, a.client_height)
        == (b.client_left, b.client_top, b.client_width, b.client_height)
        and a.dpi_scale == b.dpi_scale
    )
