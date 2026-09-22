"""Enumerate and resolve target windows."""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
from dataclasses import dataclass, asdict
from typing import Iterator

import win32con
import win32gui
import win32process

import gamelens  # noqa: F401  -- import for the DPI side effect
from gamelens.dpi import scale_for_window

_DWMWA_EXTENDED_FRAME_BOUNDS = 9
_dwmapi = ctypes.windll.dwmapi
_user32 = ctypes.windll.user32
_kernel32 = ctypes.windll.kernel32

_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


@dataclass(frozen=True)
class WindowInfo:
    hwnd: int
    title: str
    cls: str
    pid: int
    exe: str
    # Visible frame in *physical* screen pixels, DWM extended bounds.
    left: int
    top: int
    width: int
    height: int
    # Client area size, physical pixels.
    client_width: int
    client_height: int
    minimized: bool
    foreground: bool
    dpi_scale: float

    def to_dict(self) -> dict:
        return asdict(self)

    @property
    def label(self) -> str:
        return f"{self.title or '(untitled)'}  [{self.exe or '?'}]  {self.width}x{self.height}"


def extended_frame_bounds(hwnd: int) -> tuple[int, int, int, int]:
    """True visible window rect (L, T, R, B) in physical pixels.

    ``GetWindowRect`` includes the invisible resize border DWM draws outside the
    painted frame -- roughly 7px per side on Windows 10. Using it as the capture
    origin shifts every mapped click by that amount, so prefer the DWM bounds and
    fall back only if the call fails (it does for some non-composited windows).
    """
    rect = wt.RECT()
    hr = _dwmapi.DwmGetWindowAttribute(
        wt.HWND(hwnd),
        wt.DWORD(_DWMWA_EXTENDED_FRAME_BOUNDS),
        ctypes.byref(rect),
        ctypes.sizeof(rect),
    )
    if hr != 0:  # S_OK == 0
        return win32gui.GetWindowRect(hwnd)
    return rect.left, rect.top, rect.right, rect.bottom


def _exe_for_pid(pid: int) -> str:
    handle = _kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ""
    try:
        size = wt.DWORD(1024)
        buf = ctypes.create_unicode_buffer(size.value)
        # QueryFullProcessImageNameW works across the 32/64-bit boundary where
        # GetModuleFileNameEx fails with ERROR_PARTIAL_COPY.
        if _kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return buf.value.rsplit("\\", 1)[-1]
        return ""
    finally:
        _kernel32.CloseHandle(handle)


def _is_capturable(hwnd: int) -> bool:
    if not win32gui.IsWindowVisible(hwnd):
        return False
    if win32gui.GetParent(hwnd):
        return False
    style = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)
    if style & win32con.WS_EX_TOOLWINDOW:
        return False
    # Cloaked windows (UWP suspended, virtual-desktop hidden) look visible but
    # capture as an empty surface.
    cloaked = wt.DWORD()
    if _dwmapi.DwmGetWindowAttribute(
        wt.HWND(hwnd), wt.DWORD(14), ctypes.byref(cloaked), ctypes.sizeof(cloaked)
    ) == 0 and cloaked.value:
        return False
    left, top, right, bottom = extended_frame_bounds(hwnd)
    return (right - left) > 64 and (bottom - top) > 64


def describe(hwnd: int) -> WindowInfo:
    left, top, right, bottom = extended_frame_bounds(hwnd)
    _, _, cr, cb = win32gui.GetClientRect(hwnd)
    try:
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
    except Exception:
        pid = 0
    return WindowInfo(
        hwnd=hwnd,
        title=win32gui.GetWindowText(hwnd),
        cls=win32gui.GetClassName(hwnd),
        pid=pid,
        exe=_exe_for_pid(pid) if pid else "",
        left=left,
        top=top,
        width=right - left,
        height=bottom - top,
        client_width=cr,
        client_height=cb,
        minimized=bool(win32gui.IsIconic(hwnd)),
        foreground=win32gui.GetForegroundWindow() == hwnd,
        dpi_scale=scale_for_window(hwnd),
    )


def iter_windows(include_untitled: bool = False) -> Iterator[WindowInfo]:
    found: list[int] = []

    def _cb(hwnd: int, _) -> bool:
        if _is_capturable(hwnd):
            found.append(hwnd)
        return True

    win32gui.EnumWindows(_cb, None)
    for hwnd in found:
        info = describe(hwnd)
        if info.title or include_untitled:
            yield info


def list_windows(include_untitled: bool = False) -> list[WindowInfo]:
    """Capturable top-level windows, largest first (the game is usually biggest)."""
    return sorted(
        iter_windows(include_untitled),
        key=lambda w: w.width * w.height,
        reverse=True,
    )


class AmbiguousTarget(LookupError):
    """More than one window matched, so no single target can be resolved."""

    def __init__(self, query: str, matches: list[WindowInfo]) -> None:
        self.matches = matches
        listing = "\n".join(
            f"    hwnd={w.hwnd}  {w.width}x{w.height}  {w.title!r}  [{w.exe}]"
            for w in matches
        )
        super().__init__(
            f"{len(matches)} windows match {query!r}; refusing to guess.\n{listing}\n"
            f"  Pass the hwnd directly to choose one."
        )


def find_window(query: str) -> WindowInfo:
    """Resolve exactly one window by hwnd, exact title, or case-insensitive substring.

    Ambiguity raises. Picking the largest match would be an arbitrary choice made
    before any later identity check runs, and the whole safety story depends on the
    captured window and the injected window being the same one -- a guess here is
    not something a downstream check can repair.
    """
    if query.isdigit() and win32gui.IsWindow(int(query)):
        return describe(int(query))

    candidates = list_windows()

    exact = [w for w in candidates if w.title == query]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        raise AmbiguousTarget(query, exact)

    needle = query.casefold()
    hits = [w for w in candidates if needle in w.title.casefold() or needle in w.exe.casefold()]
    if not hits:
        raise LookupError(
            f"no window matching {query!r}. Open ones: "
            + ", ".join(repr(w.title) for w in candidates[:10])
        )
    if len(hits) > 1:
        raise AmbiguousTarget(query, hits)
    return hits[0]


def title_owners(title: str) -> set[int]:
    """HWNDs of every capturable window currently carrying this exact title."""
    return {w.hwnd for w in iter_windows() if w.title == title}


def title_still_owned_by(hwnd: int, title: str) -> bool:
    """True only when *hwnd* is the sole owner of *title*, and still wears it.

    Uniqueness alone is not enough, and this is the trap: suppose the target was
    titled "Game" at startup, then renames itself to "Other" while an unrelated
    same-sized window takes the name "Game". A uniqueness check still passes, the
    target is still alive, and the dimensions still match -- yet the title now
    belongs to a different window, which is the one a title-binding capture
    backend would attach to. So we require the owner set to be exactly {hwnd}
    *and* the window's current title to still equal the snapshot.

    This narrows the window of error; it does not close it. Between this check and
    a bind, ownership can still change. That residual gap is the documented
    "identity is evidence, not proof" limitation of title-based binding.
    """
    if not is_alive(hwnd):
        return False
    try:
        if win32gui.GetWindowText(hwnd) != title:
            return False
    except Exception:
        return False
    return title_owners(title) == {hwnd}


def is_alive(hwnd: int) -> bool:
    return bool(win32gui.IsWindow(hwnd))
