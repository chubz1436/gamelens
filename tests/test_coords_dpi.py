"""Coordinate mapping and DPI context tests.

Both subjects share a failure mode: nothing raises, the numbers just come back
wrong, and the symptom appears much later as a click landing next to its target.
"""

from __future__ import annotations

import ctypes
import threading
import time

import pytest
import win32gui

from gamelens.coords import (
    FrameBasis,
    Geometry,
    GeometryStale,
    GeometryTracker,
    VirtualDesktop,
    snapshot,
    virtual_desktop,
)
from gamelens.dpi import require_trustworthy_dpi, thread_dpi_state
from gamelens.input import normalize_absolute
from gamelens.windows import list_windows


@pytest.fixture
def hwnd() -> int:
    windows = list_windows()
    if not windows:
        pytest.skip("no capturable window on this machine")
    return windows[0].hwnd


# --- GL-012: staleness must compare placement, not timestamps ---------------


def test_unchanged_window_is_not_stale(hwnd):
    """Dataclass equality would compare captured_at and always report stale."""
    geometry = snapshot(hwnd)
    time.sleep(0.05)
    fresh = snapshot(hwnd)
    assert fresh.captured_at != geometry.captured_at, "timestamps should differ"
    assert geometry.matches_current(), "an unmoved window must not read as stale"


def test_generation_is_stable_while_the_window_sits_still(hwnd):
    tracker = GeometryTracker(hwnd)
    first = tracker.generation
    for _ in range(5):
        tracker.refresh()
    assert tracker.generation == first


def test_moved_window_is_detected(hwnd):
    """Synthesised rather than actually dragging a window, which tests cannot do."""
    geometry = snapshot(hwnd)
    moved = Geometry(**{**geometry.__dict__, "dwm_left": geometry.dwm_left + 50})
    from gamelens.coords import _same_placement
    assert not _same_placement(geometry, moved)


# --- frame basis is measured, not assumed ----------------------------------


def test_basis_resolves_for_known_sizes(hwnd):
    geometry = snapshot(hwnd)
    assert geometry.basis_for(geometry.dwm_width, geometry.dwm_height) in FrameBasis


def test_unknown_frame_size_refuses_to_map(hwnd):
    """An unrecognised size means a resize or a scaling backend -- not a guess."""
    geometry = snapshot(hwnd)
    with pytest.raises(GeometryStale):
        geometry.basis_for(12345, 6789)


def test_frame_screen_round_trip(hwnd):
    geometry = snapshot(hwnd)
    w, h = geometry.dwm_width, geometry.dwm_height
    for point in [(0, 0), (10, 10), (w - 1, h - 1)]:
        sx, sy = geometry.frame_to_screen(*point, w, h)
        assert geometry.screen_to_frame(sx, sy, w, h) == point


def test_dwm_bounds_differ_from_window_rect_on_framed_windows():
    """Windows 10 reports ~7px of invisible resize border around framed windows.

    Using GetWindowRect as the capture origin would shift every mapped click by
    that amount. Skipped if only borderless windows are open.
    """
    framed = [
        snapshot(w.hwnd) for w in list_windows()
        if snapshot(w.hwnd).dwm_left != snapshot(w.hwnd).win_left
    ]
    if not framed:
        pytest.skip("no framed windows open to demonstrate the border")
    for geometry in framed:
        assert geometry.dwm_left > geometry.win_left


# --- GL-001: absolute normalization must subtract the origin ----------------


def test_normalization_spans_the_real_desktop():
    vd = virtual_desktop()
    assert normalize_absolute(vd.left, vd.top, vd) == (0, 0)
    assert normalize_absolute(vd.right - 1, vd.bottom - 1, vd) == (65535, 65535)


def test_negative_origin_desktop_is_handled():
    """A monitor left of the primary makes the desktop origin negative.

    The naive formula (x * 65535 / width) sends screen x=0 to the far left edge
    of the *other* monitor. The correct one puts it in the middle.
    """
    vd = VirtualDesktop(left=-1920, top=0, width=3840, height=1080)
    assert normalize_absolute(-1920, 0, vd) == (0, 0)
    assert normalize_absolute(3839 - 1920, 0, vd) == (65535, 0)
    middle, _ = normalize_absolute(0, 0, vd)
    assert 32000 < middle < 33000, "screen x=0 sits mid-desktop, not at the edge"


def test_coordinates_outside_the_desktop_are_refused():
    vd = VirtualDesktop(left=0, top=0, width=1920, height=1080)
    with pytest.raises(ValueError):
        normalize_absolute(5000, 0, vd)
    with pytest.raises(ValueError):
        normalize_absolute(-1, 0, vd)


# --- GL-013: DPI awareness is per thread ------------------------------------


def test_main_thread_is_trustworthy():
    assert thread_dpi_state().trustworthy
    require_trustworthy_dpi()


def test_worker_thread_lower_context_is_refused():
    """A cached import-time measurement would let this worker through.

    The worker adopts DPI_AWARENESS_CONTEXT_UNAWARE, under which every rect it
    reads is virtualized to 96 DPI. The guard must measure the calling thread,
    not remember what some other thread had.
    """
    result: dict = {}

    def worker() -> None:
        ctypes.windll.user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(-1))
        result["state"] = thread_dpi_state()
        try:
            require_trustworthy_dpi()
            result["guard"] = "passed"
        except RuntimeError as exc:
            result["guard"] = str(exc)

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join()

    assert not result["state"].trustworthy
    assert result["guard"] != "passed", "a DPI-unaware thread must not be allowed to map"
    assert thread_dpi_state().trustworthy, "the main thread is unaffected"
